# sqlcipher3, rebuilt (#682)

Clinical builds encrypt `scans.db` with SQLCipher through the `sqlcipher3`
module (`omotion.db_open`). Version 0.6.2 on PyPI statically links SQLCipher
4.12.0 (SQLite 3.51.1) and OpenSSL 3.6.0. No newer wheel exists, and the
OpenSSL 3.6 branch reaches end of life on 2026-11-01. The wheels in this
directory are the same sqlcipher3 0.6.2 source, rebuilt per its README against
**SQLCipher 4.19.0 (SQLite 3.53.4)** and **OpenSSL 3.5.9 LTS**.

| File | What it is |
|---|---|
| `sqlcipher3-0.6.2+sqlcipher4.19.0.openssl3.5.9-cp312-cp312-win_amd64.whl` | For the release build (Python 3.12, `conda-win-64.lock`) |
| `sqlcipher3-0.6.2+sqlcipher4.19.0.openssl3.5.9-cp313-cp313-win_amd64.whl` | For the dev setup (Python 3.13) |
| `SHA256SUMS` | Hashes of the two wheels, as the build run recorded them |
| `pins.txt` | Every input of the build, plus the versions the result must report |
| `conanfile.py` | Replaces upstream's: OpenSSL `3.5.9` instead of `3.6.0`, plus two hardening options (below) |
| `check_sqlcipher.py` | Checks the installed module against `pins.txt`; the Windows build and the build run both call it |
| `roundtrip.py` | Writes or reads an encrypted DB the way `omotion.db_open` does; used by the build run |

The version's local label (`+sqlcipher4.19.0.openssl3.5.9`) puts the real
library versions into `pip show`, `pip freeze` and the build's CycloneDX SBOM.
It still satisfies `sqlcipher3==0.6.2` and the SDK's `sqlcipher3>=0.6`.

## How the build uses them

- `requirements.txt` installs the wheel that matches the Windows Python (3.12
  or 3.13). Nothing installs the PyPI wheel any more. A Windows Python other
  than 3.12 or 3.13 gets no sqlcipher3, so use one of those two for anything
  that builds or runs Clinical. macOS gets no sqlcipher3 either: the Mac
  build is research-only and the SDK refuses the keystore there.
- `.github/actions/windows-build/action.yml` runs `check_sqlcipher.py` right
  after `pip install -r requirements.txt`. It fails the build unless the
  module that is about to be frozen reports the package version and the
  SQLCipher, SQLite and OpenSSL versions in `pins.txt`. The PyPI wheel fails
  all four checks.
- `tests/test_sqlcipher_rebuild.py` pins that wiring and the hashes. It also
  opens `tests/fixtures/scans_sqlcipher-4.12.0.sqlcipher` through
  `omotion.db_open`: a scan DB that the shipped stack (SDK 1.12.0 +
  sqlcipher3 0.6.2 from PyPI) wrote.

## How the wheels are built

`.github/workflows/sqlcipher3-wheels.yml` runs on any push that changes
`pins.txt`, `conanfile.py`, `roundtrip.py`, `check_sqlcipher.py` or the
workflow itself. Once the workflow is on `main` it can also be started with
workflow_dispatch.

1. **Amalgamation** (ubuntu-24.04). Clones sqlcipher3 and SQLCipher at the
   pinned commits. Then it runs sqlcipher3's own `vendor/update` script (the
   `configure` + `make sqlite3.c sqlite3.h` recipe upstream uses).
   - **Recipe check:** it first regenerates SQLCipher **4.12.0** that way and
     requires a byte-identical match with the `vendor/sqlite3.[ch]` that
     upstream committed for 0.6.2. That proves it is the recipe upstream
     used to make them.
   - It then generates **4.19.0**, checks the version defines, and records
     the sha256 of both files.
2. **Wheels** (windows-latest, MSVC, Python 3.12 and 3.13).
   - Drops the amalgamation and this `conanfile.py` into the pinned sqlcipher3
     checkout, then stamps the version in `setup.py` and `pyproject.toml`.
   - Runs `pip wheel --no-build-isolation` with the pinned conan and
     setuptools. The conan step builds OpenSSL from source, because the
     non-default options rule out conancenter's prebuilt binaries. The
     extension links that static OpenSSL.
3. **Checks on each wheel**, installed into a clean venv:
   - `check_sqlcipher.py` must pass.
   - The `OPENSSLDIR` compiled into the `.pyd` must be the conanfile value.
   - A DB written by the PyPI wheel must open with the rebuilt one, and one
     written by the rebuilt wheel must open with the PyPI one. A rollback to a
     build that still ships the PyPI wheel can therefore read what this one
     wrote. Both directions pass `PRAGMA cipher_integrity_check` and
     `integrity_check`, and the rows must match.

The run uploads the amalgamation and one artifact per wheel. Each wheel
artifact holds the wheel, its `SHA256SUMS`, and `conan-openssl.json`, which
records the OpenSSL recipe revision and package id. Workflow artifacts expire,
so the wheels that ship are the ones committed here.

### OpenSSL hardening

SQLCipher never initialises OpenSSL, so libcrypto's implicit init loads
`openssl.cnf` from the compiled-in `OPENSSLDIR`. A config file can load
provider DLLs. The PyPI wheel's `OPENSSLDIR` is the build runner's conan cache
(`C:\Users\runneradmin\.conan2\p\b\opens…\p\res`). `conanfile.py` therefore
sets two options:

- `no_autoload_config`: libcrypto reads no config file.
- `openssldir`: `C:\Program Files\Common Files\SSL`, OpenSSL's own
  admin-only default for Windows builds.

Neither option changes what SQLCipher calls.

## Current wheels

Built by run
[37072314799](https://github.com/OpenwaterHealth/openmotion-bloodflow-app/actions/runs/37072314799)
on commit `12ad170` (2026-10-02), on `windows-latest` with MSVC 19.5
(conan `compiler.version=195`, dynamic CRT).

| | |
|---|---|
| sqlcipher3 source | coleifer/sqlcipher3 `14fc2632` (tag 0.6.2) |
| SQLCipher source | sqlcipher/sqlcipher `c4b275a4` (tag v4.19.0) |
| `sqlite3.c` sha256 | `f6d54c906fefdc09124c845cb8f45145a8049aee32c0e5aad95c042b9e4dc8d3` |
| `sqlite3.h` sha256 | `8a9d1bff44d75174ca6dea3ea9bac50a6104d86facb566647b8bb839375b7b3a` |
| OpenSSL | conancenter `openssl/3.5.9`, recipe revision `e9977d17028841f6971240542d1f7b11`, package id `2735f6d5d87a0cd4b293398da524747033f11231`, built from source |
| Reported | `4.19.0 community` / SQLite `3.53.4` / `OpenSSL 3.5.9 29 Sep 2026` |
| Compiled-in paths | `OPENSSLDIR` `C:\Program Files\Common Files\SSL`, `ENGINESDIR` `\lib\engines-3`, `MODULESDIR` `\lib\ossl-modules` |

The wheel hashes are in `SHA256SUMS`. Only the `.pyd` differs from the PyPI
wheel: its `__init__.py` and `dbapi2.py` are identical. The amalgamation is
reproducible: an earlier run from the same pins (37071437638) produced the same
two hashes. The wheels are not bit-reproducible, because MSVC stamps a time
into the PE headers.

`ENGINESDIR` and `MODULESDIR` are drive-relative, as they are in the PyPI
wheel. That comes from the recipe's `--prefix=/`. OpenSSL reads them only when
asked to load an engine or provider module. SQLCipher never asks, and with
the config autoload off, no config file can ask either.

## Bumping a version

1. Edit `pins.txt`, and change the OpenSSL version in `conanfile.py` to
   match.
   - For SQLCipher, set the tag and the commit it points to:
     `gh api repos/sqlcipher/sqlcipher/git/ref/tags/<tag>`, then follow an
     annotated tag object to its commit. Also update `SQLITE_VERSION` from the
     tag's `VERSION` file.
   - For sqlcipher3, set its new tag and commit, and the
     `UPSTREAM_SQLCIPHER_*` pins to the SQLCipher release that sqlcipher3
     version vendors.
   - Update `PACKAGE_VERSION` to match.
2. Push the branch. The workflow builds and checks both wheels.
3. Download the run's wheel artifacts. Replace the wheels here and
   regenerate `SHA256SUMS` from the artifacts' own hash files. Update the
   file names in `requirements.txt`, and fill in "Current wheels" above.
4. Run `pytest tests/test_sqlcipher_rebuild.py`. Re-run
   `pip install -r requirements.txt` in your env first, so the fixture opens
   with the new module.
