"""Unit tests for MotionConnector._version_newer.

Regression for the update-check false positive: a setuptools_scm dev
version like '1.2.0-dev.1-0-gabc1234-dirty' failed the old int() parse,
fell back to [0], and made every remote release look newer (the app
offered "update" 1.1.3 over a local 1.2.0-dev build).
"""

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit

newer = MotionConnector._version_newer


def test_plain_upgrade():
    assert newer("1.2.0", "1.1.3") is True


def test_plain_no_downgrade():
    assert newer("1.1.3", "1.2.0") is False


def test_equal_versions():
    assert newer("1.1.3", "1.1.3") is False


def test_dev_local_not_downgraded_by_older_release():
    # The bug from the 2026-06-10 log: local 1.2.0-dev must not be
    # "upgraded" to release 1.1.3.
    assert newer("1.1.3", "1.2.0-dev.1-0-gb9f5cdb-dirty") is False


def test_dev_local_upgraded_by_same_base_release():
    # A dev build is a pre-release of its base version.
    assert newer("1.2.0", "1.2.0-dev.1-0-gb9f5cdb-dirty") is True


def test_dev_local_upgraded_by_newer_release():
    assert newer("1.3.0", "1.2.0-dev.1-0-gb9f5cdb-dirty") is True


def test_pre_prefix_local():
    assert newer("0.4.3", "pre-0.4.3") is True
    assert newer("0.4.2", "pre-0.4.3") is False


def test_inline_pre_suffix():
    assert newer("1.0", "1.0-pre3") is True
    assert newer("1.0-pre3", "1.0") is False


def test_rc_suffix_counts_as_prerelease():
    assert newer("1.5.8", "1.5.8-rc.2") is True
    assert newer("1.5.8-rc.2", "1.5.8") is False


def test_newer_prerelease_of_same_base():
    # Beta channel (#386): a newer rc/dev of the SAME numeric base must be
    # offered, or the app self-updater never advances rc.1 -> rc.2.
    assert newer("1.5.8-rc.2", "1.5.8-rc.1") is True
    assert newer("1.5.8-dev.1", "1.5.8-dev.0") is True
    assert newer("1.5.8-rc.1", "1.5.8-dev.0") is True   # rc outranks dev


def test_not_newer_same_or_older_prerelease():
    assert newer("1.5.8-rc.1", "1.5.8-rc.2") is False
    assert newer("1.5.8-rc.1", "1.5.8-rc.1") is False
    assert newer("1.5.8-dev.0", "1.5.8-rc.1") is False  # dev below rc
