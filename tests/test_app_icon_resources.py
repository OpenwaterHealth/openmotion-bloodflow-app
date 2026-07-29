"""Guards for the app icon (issue #223).

The taskbar showed the generic Windows icon intermittently because Qt
registers its window classes with ``LoadImage(hInst, L"IDI_ICON1", ...)`` and
falls back to ``IDI_APPLICATION`` when that name is absent — which it always
was, since PyInstaller publishes the icon group under an *integer* id. These
tests lock down both halves of the fix: the icon asset itself, and the build
wiring that publishes the named resource.
"""

import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ICO_PATH = REPO_ROOT / "assets" / "images" / "favicon.ico"
SPEC_PATH = REPO_ROOT / "openwater.spec"

sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _parse_ico(data: bytes):
    """Return [(width, height, n_bytes, offset, is_png), ...]."""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and kind == 1, "not an ICO file"
    frames = []
    for i in range(count):
        entry = data[6 + i * 16: 6 + (i + 1) * 16]
        w, h, _colors, _rsv, _planes, _bpp, size, offset = struct.unpack(
            "<BBBBHHII", entry
        )
        blob = data[offset:offset + size]
        frames.append(
            (w or 256, h or 256, size, offset, blob[:8] == b"\x89PNG\r\n\x1a\n")
        )
    return frames


@pytest.mark.unit
def test_favicon_ico_is_structurally_valid():
    data = ICO_PATH.read_bytes()
    frames = _parse_ico(data)

    assert {f[0] for f in frames} == {16, 24, 32, 48, 64, 128, 256}
    for width, height, size, offset, _is_png in frames:
        assert width == height, f"non-square {width}x{height} frame"
        assert size > 0
        assert offset + size <= len(data), "frame runs past EOF"
    # The last frame must end exactly at EOF — a short file is the classic
    # symptom of a binary mangled by CRLF translation on checkout.
    assert max(o + s for _w, _h, s, o, _p in frames) == len(data)


@pytest.mark.unit
def test_favicon_ico_is_reproducible_from_the_generator(tmp_path):
    """scripts/make_app_icon.py must still reproduce the committed bytes.

    The generator hand-splices the ICO directory; this catches a regression in
    that arithmetic, and catches someone regenerating the asset with a plain
    Pillow ``Image.save(..., format="ICO")``.
    """
    pytest.importorskip("PIL")

    work = tmp_path / "repo"
    (work / "assets" / "images").mkdir(parents=True)
    (work / "scripts").mkdir()
    for rel in ("assets/images/favicon.png", "scripts/make_app_icon.py"):
        (work / rel).write_bytes((REPO_ROOT / rel).read_bytes())

    subprocess.run(
        [sys.executable, str(work / "scripts" / "make_app_icon.py")],
        check=True, capture_output=True,
    )

    assert (work / "assets" / "images" / "favicon.ico").read_bytes() == \
        ICO_PATH.read_bytes(), (
            "regenerating favicon.ico no longer reproduces the committed file"
        )


@pytest.mark.unit
def test_spec_anchors_icon_to_specpath_not_cwd():
    """ICON_FILE must not resolve against the build-time CWD.

    ``os.path.abspath("assets/images/favicon.ico")`` silently produced a
    nonexistent path when PyInstaller was invoked from another directory.
    """
    spec = SPEC_PATH.read_text(encoding="utf-8")
    assert 'os.path.abspath("assets/images/favicon.ico")' not in spec
    assert "SPECPATH" in spec.split("ICON_FILE =")[1].split("\n")[0]


@pytest.mark.unit
def test_spec_installs_the_named_group_icon_hook():
    """The build must publish the icon group under Qt's "IDI_ICON1" name.

    Without this, Qt registers every window class with the generic Windows
    icon and a slow startup strands it on the taskbar button (#223).
    """
    spec = SPEC_PATH.read_text(encoding="utf-8")
    assert "install_pyinstaller_hook()" in spec
    assert "has_named_group_icon" in spec, "the post-build assertion was dropped"


@pytest.mark.unit
def test_spec_fails_loudly_on_missing_resources():
    """Missing QML/assets must abort the build, not ship a broken artifact."""
    spec = SPEC_PATH.read_text(encoding="utf-8")
    assert spec.count("raise SystemExit") >= 3


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="Windows resource APIs")
def test_named_group_icon_probe_rejects_a_stock_python_exe():
    """The probe must actually discriminate, not just return True.

    python.exe has icon resources but not one named "IDI_ICON1", so it is a
    true negative — if this ever passes, the probe is broken and the build
    assertion it backs is worthless.
    """
    from win_icon_resource import group_icon_names, has_named_group_icon

    assert has_named_group_icon(sys.executable) is False
    # ...and the enumerator still sees python.exe's own icon group, proving we
    # are reading real resources rather than failing to open the file at all.
    assert group_icon_names(sys.executable), "no RT_GROUP_ICON found at all"


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="Windows resource APIs")
def test_built_exe_has_both_icon_groups():
    """If a build is present, it must carry the named *and* numeric groups.

    The numeric group #1 is what Explorer shows for the file itself; the named
    one is what Qt looks up for the window class. Losing either is a bug.
    """
    from win_icon_resource import group_icon_names, has_named_group_icon

    exe = REPO_ROOT / "dist" / "Open-Motion" / "Open-Motion.exe"
    if not exe.exists():
        pytest.skip("no build in dist/ — run PyInstaller first")

    names = group_icon_names(exe)
    assert ("STR", "IDI_ICON1") in names, f"missing Qt class icon; got {names}"
    assert ("INT", 1) in names, f"PyInstaller's file icon was clobbered: {names}"
    assert has_named_group_icon(exe)
