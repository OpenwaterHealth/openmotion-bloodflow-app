"""Check the installed sqlcipher3 against pins.txt next to this file (#682).

The Windows build runs this right after installing requirements.txt:

    python vendor/sqlcipher3/check_sqlcipher.py

It fails the build unless the sqlcipher3 that will be frozen into the app is
the rebuilt one: the package version carries the rebuild's local label, and
the module itself reports the pinned SQLCipher, SQLite and OpenSSL versions.
The PyPI wheel (SQLCipher 4.12.0, OpenSSL 3.6.0) fails all four.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import sys
from pathlib import Path

PINS_FILE = Path(__file__).resolve().with_name("pins.txt")


def read_pins(path: Path = PINS_FILE) -> dict[str, str]:
    """KEY=VALUE lines of pins.txt; blank lines and # comments skipped."""
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key or not value:
            raise ValueError(f"{path.name}: not a KEY=VALUE line: {line!r}")
        pins[key] = value
    return pins


def installed() -> dict[str, str]:
    """What the importable sqlcipher3 reports about itself."""
    from sqlcipher3 import dbapi2

    conn = dbapi2.connect(":memory:")
    try:
        # The cipher_* PRAGMAs answer only once a key is set.
        conn.execute("PRAGMA key = 'version-check'")

        def pragma(name: str) -> str:
            return conn.execute(f"PRAGMA {name}").fetchone()[0]

        return {
            "package": importlib.metadata.version("sqlcipher3"),
            "sqlcipher": pragma("cipher_version"),          # "4.19.0 community"
            "sqlite": dbapi2.sqlite_version,                 # "3.53.4"
            "provider": pragma("cipher_provider"),          # "openssl"
            "openssl": pragma("cipher_provider_version"),   # "OpenSSL 3.5.9 <date>"
        }
    finally:
        conn.close()


def mismatches(pins: dict[str, str], got: dict[str, str]) -> list[str]:
    """One line per reported value that is not the pinned one."""
    want = {
        "package": pins["PACKAGE_VERSION"],
        "sqlcipher": pins["SQLCIPHER_VERSION"],
        "sqlite": pins["SQLITE_VERSION"],
        "provider": "openssl",
        "openssl": pins["OPENSSL_VERSION"],
    }
    def word(text: str, index: int) -> str:
        parts = text.split()
        return parts[index] if len(parts) > index else text

    # Compare version tokens only: "4.19.0 community" -> "4.19.0",
    # "OpenSSL 3.5.9 30 Sep 2026" -> "3.5.9".
    version = dict(got, sqlcipher=word(got["sqlcipher"], 0), openssl=word(got["openssl"], 1))
    return [
        f"{key}: expected {expected}, got {got[key]!r}"
        for key, expected in want.items()
        if version[key] != expected
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pins-file", type=Path, default=PINS_FILE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    pins = read_pins(args.pins_file)
    try:
        got = installed()
    except ImportError:
        print("::error ::sqlcipher3 is not installed in this environment")
        return 1
    for key, value in got.items():
        print(f"sqlcipher3 {key}: {value}")
    bad = mismatches(pins, got)
    for line in bad:
        print(f"::error ::sqlcipher3 {line} (vendor/sqlcipher3/pins.txt, #682)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
