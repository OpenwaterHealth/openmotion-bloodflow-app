"""Pure-software unit test for the engineering-mode password check.

Does NOT request the ``app`` fixture, so the bloodflow app is not
launched and no hardware is touched — only the lightweight
session-autouse QCoreApplication fixture runs.
"""
from motion_connector import engineering_password_matches, _ENGINEERING_PASSWORD


def test_correct_password_matches():
    assert engineering_password_matches(_ENGINEERING_PASSWORD) is True


def test_wrong_password_rejected():
    assert engineering_password_matches("nope") is False


def test_empty_password_rejected():
    assert engineering_password_matches("") is False


def test_none_password_rejected():
    assert engineering_password_matches(None) is False
