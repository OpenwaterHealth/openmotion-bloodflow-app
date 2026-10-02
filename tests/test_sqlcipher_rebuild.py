"""The rebuilt sqlcipher3 (#682).

sqlcipher3 0.6.2 from PyPI links SQLCipher 4.12.0 and OpenSSL 3.6.0. On
Windows, requirements.txt installs the wheels vendored in vendor/sqlcipher3/
instead, rebuilt against SQLCipher 4.19.0 and OpenSSL 3.5.9 by
.github/workflows/sqlcipher3-wheels.yml, and the Windows build checks the
result with vendor/sqlcipher3/check_sqlcipher.py. These tests pin that
wiring, and that a scan database written before the rebuild still opens with
whichever sqlcipher3 is installed.

The fixture, tests/fixtures/scans_sqlcipher-4.12.0.sqlcipher, was written once
by the stack 1.5.3 ships: openmotion-sdk 1.12.0 ``ScanDatabase`` (schema v2)
over sqlcipher3 0.6.2 from PyPI, encryption policy on, key ``FIXTURE_KEY``. It
holds one closed session and 640 synthetic ``session_data`` rows (40 frames x
2 sides x 8 cameras). Never regenerate it with a newer SQLCipher: a database
that predates the upgrade is the whole point.
"""

import hashlib
import re
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR = REPO_ROOT / "vendor" / "sqlcipher3"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "scans_sqlcipher-4.12.0.sqlcipher"
# Test-only key of the synthetic fixture above; it protects nothing.
FIXTURE_KEY = "6f6d6f74696f6e2d363832207465737420666978747572652c206e6f74206120"

sys.path.insert(0, str(VENDOR))

import check_sqlcipher  # noqa: E402


@pytest.fixture(scope="module")
def pins() -> dict:
    return check_sqlcipher.read_pins()


# ------------------------------------------------ databases from before #682
def test_scan_db_written_by_sqlcipher_4_12_still_opens(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    from omotion import db_key, db_open

    db = tmp_path / "scans.db"
    shutil.copyfile(FIXTURE, db)
    monkeypatch.setattr(db_key, "_require_encryption", True)
    monkeypatch.setattr(db_key, "get_key", lambda create=False: FIXTURE_KEY)

    conn = db_open.connect(db, create_ok=False)
    try:
        assert conn.execute("PRAGMA cipher_integrity_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        session = conn.execute(
            "SELECT session_label, session_start, session_end FROM sessions"
        ).fetchall()
        assert [tuple(row) for row in session] == [("fixture-682", 1759446000.0, 1759446001.0)]
        count, frames, cams = conn.execute(
            "SELECT count(*), count(DISTINCT frame_id), count(DISTINCT cam_id) FROM session_data"
        ).fetchone()
        assert (count, frames, cams) == (640, 40, 8)
        bfi = conn.execute(
            "SELECT bfi FROM session_data WHERE cam_id = 7 AND side = 1 AND frame_id = 39"
        ).fetchone()[0]
        assert bfi == pytest.approx(1.0 + 7 * 0.1 + 39 * 0.001)
    finally:
        conn.close()


def test_the_fixture_is_encrypted_and_unchanged():
    data = FIXTURE.read_bytes()
    assert not data.startswith(b"SQLite format 3\x00")
    assert hashlib.sha256(data).hexdigest() == (
        "44c175340145e0927b92a7fb355dd7fad59ca541a06ea00fa165929e7e9300b0"
    )


# --------------------------------------------------- the vendored wheels
def _wheels() -> list[Path]:
    return sorted(VENDOR.glob("sqlcipher3-*.whl"))


def test_vendored_wheels_carry_the_pinned_versions(pins):
    """One win_amd64 wheel per supported Python, named for the pins."""
    version = pins["PACKAGE_VERSION"]
    assert version == (
        f"{pins['SQLCIPHER3_VERSION']}+sqlcipher{pins['SQLCIPHER_VERSION']}"
        f".openssl{pins['OPENSSL_VERSION']}"
    )
    assert [w.name for w in _wheels()] == [
        f"sqlcipher3-{version}-cp{tag}-cp{tag}-win_amd64.whl" for tag in ("312", "313")
    ]


def test_vendored_wheels_match_their_recorded_hashes():
    recorded = {}
    for line in (VENDOR / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        recorded[name.lstrip("*")] = digest
    assert recorded == {
        w.name: hashlib.sha256(w.read_bytes()).hexdigest() for w in _wheels()
    }


def test_conanfile_links_the_pinned_openssl(pins):
    conanfile = (VENDOR / "conanfile.py").read_text(encoding="utf-8")
    assert re.findall(r"self\.requires\('openssl/([^']+)'\)", conanfile) == [pins["OPENSSL_VERSION"]]
    assert '"openssl/*:no_autoload_config": True' in conanfile


def test_requirements_install_the_vendored_wheels_on_windows_only(pins):
    """Windows gets the rebuilt wheel for its Python; nothing installs the
    PyPI one any more. macOS is research-only and never opens an encrypted
    database, so it gets no sqlcipher3 at all."""
    lines = [
        line.strip()
        for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if "sqlcipher3" in line and not line.lstrip().startswith("#")
    ]
    version = pins["PACKAGE_VERSION"]
    assert lines == [
        f"vendor/sqlcipher3/sqlcipher3-{version}-cp{tag}-cp{tag}-win_amd64.whl"
        f' ; sys_platform == "win32" and python_version == "{python}"'
        for tag, python in (("312", "3.12"), ("313", "3.13"))
    ]


def test_windows_build_checks_sqlcipher3_after_installing_requirements():
    action = (REPO_ROOT / ".github" / "actions" / "windows-build" / "action.yml").read_text(encoding="utf-8")
    install = action.index("pip install -r requirements.txt")
    check = action.index("python vendor/sqlcipher3/check_sqlcipher.py")
    build = action.index("- name: Build with PyInstaller")
    assert install < check < build


# ---------------------------------------------------- check_sqlcipher.py
def test_check_passes_the_rebuilt_module(pins):
    got = {
        "package": pins["PACKAGE_VERSION"],
        "sqlcipher": f"{pins['SQLCIPHER_VERSION']} community",
        "sqlite": pins["SQLITE_VERSION"],
        "provider": "openssl",
        "openssl": f"OpenSSL {pins['OPENSSL_VERSION']} 30 Sep 2026",
    }
    assert check_sqlcipher.mismatches(pins, got) == []


def test_check_fails_the_pypi_wheel(pins):
    got = {  # what sqlcipher3 0.6.2 from PyPI reports
        "package": "0.6.2",
        "sqlcipher": "4.12.0 community",
        "sqlite": "3.51.1",
        "provider": "openssl",
        "openssl": "OpenSSL 3.6.0 1 Oct 2025",
    }
    bad = check_sqlcipher.mismatches(pins, got)
    assert [line.split(":")[0] for line in bad] == ["package", "sqlcipher", "sqlite", "openssl"]


def test_check_reads_the_pins_file(tmp_path):
    pins_file = tmp_path / "pins.txt"
    pins_file.write_text("# comment\n\nA=1\nB=x+y\n", encoding="utf-8")
    assert check_sqlcipher.read_pins(pins_file) == {"A": "1", "B": "x+y"}
    pins_file.write_text("NOT A PIN\n", encoding="utf-8")
    with pytest.raises(ValueError):
        check_sqlcipher.read_pins(pins_file)
