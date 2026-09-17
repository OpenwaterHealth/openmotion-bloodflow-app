"""Report the installed openmotion-sdk version against sdk-version.txt (#545).

The release workflow runs this right after the build environment is set up:

    python scripts/check_sdk_pin.py            # dev builds: report only
    python scripts/check_sdk_pin.py --enforce  # rc / production: must match

`sdk-version.txt` is the single place the release SDK is pinned. rc and
production tags install exactly that release, so the shipped binary, the
generated SBOM and this log line all describe the same SDK. Dev builds install
openmotion-sdk@next, whose setuptools_scm stamp never equals a release, so the
pin is reported but not enforced there.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = REPO_ROOT / "sdk-version.txt"
PIN_RE = re.compile(r"^\d+\.\d+\.\d+$")
DIST_NAME = "openmotion-sdk"


def read_pin(pin_file: Path = PIN_FILE) -> str:
    """The pinned SDK release, validated as a plain X.Y.Z version."""
    pin = pin_file.read_text(encoding="utf-8").strip()
    if not PIN_RE.match(pin):
        raise ValueError(
            f"{pin_file.name} must hold one release version like 1.12.0, "
            f"got {pin!r}"
        )
    return pin


def installed_version(dist_name: str = DIST_NAME) -> str:
    return importlib.metadata.version(dist_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--enforce", action="store_true",
        help="exit 1 unless the installed version equals the pin",
    )
    parser.add_argument(
        "--pin-file", type=Path, default=PIN_FILE, help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    pin = read_pin(args.pin_file)
    try:
        got = installed_version()
    except importlib.metadata.PackageNotFoundError:
        print(f"::error ::{DIST_NAME} is not installed in this environment")
        return 1

    print(f"{DIST_NAME} installed: {got}; {args.pin_file.name} pins: {pin}")
    if args.enforce and got != pin:
        print(
            f"::error ::installed {DIST_NAME} {got} does not match the pinned "
            f"{pin}; bump {args.pin_file.name} or fix the install step"
        )
        return 1
    if not args.enforce and got != pin:
        print("(dev build: pin reported, not enforced)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
