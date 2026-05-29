"""Pure-software unit test for the developer-mode password check.

Does NOT request the ``app`` fixture, so the bloodflow app is not
launched and no hardware is touched — only the lightweight
session-autouse QCoreApplication fixture runs.
"""
from motion_connector import developer_password_matches, _DEVELOPER_PASSWORD


def test_correct_password_matches():
    assert developer_password_matches(_DEVELOPER_PASSWORD) is True


def test_wrong_password_rejected():
    assert developer_password_matches("nope") is False


def test_empty_password_rejected():
    assert developer_password_matches("") is False


def test_none_password_rejected():
    assert developer_password_matches(None) is False
