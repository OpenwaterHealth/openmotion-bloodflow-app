"""Fail a Nuitka build whose binaries carry another build's data (#698).

    python scripts/check_nuitka_blobs.py <nuitka output dir> <onefile exe>

Nuitka puts two data blobs into what it builds:

* ``<stem>.build/blobs/__constant.bin``, the program's constants, goes into
  the program binary (``<stem>.dist/<stem>.dll`` on Nuitka 4).
* ``<stem>.onefile-build/blobs/__payload.bin``, the compressed distribution
  folder, goes into the onefile exe.

Built with zig, Nuitka's default puts them in with C23 ``#embed``. zig's
compile cache records the embedded file by absolute path, while the
generated source names it relatively, so it is the same in every build
directory. A build therefore got another build directory's compiled blob
back whenever that build's blob was still on disk: a ``-KeepStandalone``
tree, or a build running in another worktree. The exe then shipped the other
build's constants or payload with no error anywhere. scripts/build_nuitka.ps1
now selects the ``coff_obj`` resource mode, in which Nuitka writes the blob
objects itself and no compiler cache is involved. This check proves the
result, whatever the mode.

A blob counts as present when its bytes appear contiguously in the binary.
Signing does not move them: Authenticode only appends to the file.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROBE = 64 * 1024


def contains(haystack: bytes, needle: bytes) -> bool:
    """True if ``needle`` appears contiguously in ``haystack``."""
    if not needle:
        return False
    head = needle[:_PROBE]
    i = haystack.find(head)
    while i >= 0:
        if haystack[i:i + len(needle)] == needle:
            return True
        i = haystack.find(head, i + 1)
    return False


def check(output_dir: str | Path, exe: str | Path, stem: str = "main") -> list[str]:
    """One error per blob that is missing or not inside its binary. An empty
    list means pass."""
    output_dir = Path(output_dir)
    errors = []

    constants = output_dir / f"{stem}.build" / "blobs" / "__constant.bin"
    programs = [
        p for p in (output_dir / f"{stem}.dist" / f"{stem}{ext}" for ext in (".dll", ".exe"))
        if p.is_file()
    ]
    if not constants.is_file():
        errors.append(f"no constants blob at {constants}")
    elif not programs:
        errors.append(f"no {stem}.dll / {stem}.exe in {output_dir / (stem + '.dist')}")
    else:
        blob = constants.read_bytes()
        if any(contains(p.read_bytes(), blob) for p in programs):
            print(f"[blobs] {programs[0].name} holds this build's constants ({len(blob)} bytes)")
        else:
            errors.append(
                f"{', '.join(p.name for p in programs)} does not hold this build's "
                f"constants ({constants}): a compile cache returned another build's object"
            )

    payload = output_dir / f"{stem}.onefile-build" / "blobs" / "__payload.bin"
    exe = Path(exe)
    if not payload.is_file():
        errors.append(f"no onefile payload at {payload}")
    elif not exe.is_file():
        errors.append(f"no onefile exe at {exe}")
    else:
        blob = payload.read_bytes()
        if contains(exe.read_bytes(), blob):
            print(f"[blobs] {exe.name} holds this build's payload ({len(blob)} bytes)")
        else:
            errors.append(
                f"{exe} does not hold this build's onefile payload ({payload}): "
                "a compile cache returned another build's object"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    stem = "main"
    if len(args) == 4 and args[2] == "--stem":
        stem = args[3]
        args = args[:2]
    if len(args) != 2:
        print("usage: python scripts/check_nuitka_blobs.py <nuitka output dir> <onefile exe> [--stem NAME]")
        return 2
    errors = check(args[0], args[1], stem)
    for error in errors:
        print(f"::error ::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
