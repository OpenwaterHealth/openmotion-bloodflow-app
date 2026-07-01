"""Bug-report builder — pure helpers, no I/O (no SMTP, no shell, no Qt)."""

from urllib.parse import parse_qs, urlsplit

import pytest

import bug_report

pytestmark = pytest.mark.unit


def test_build_report_text_includes_code_and_context():
    text = bug_report.build_report_text(
        code="E-101",
        title="Sensor self-check failed",
        message="One or more devices did not respond.",
        timestamp="2026-06-15 23:00:00",
        app_version="1.2.3",
        device_info="left=ABC123 console=XYZ",
        log_path=r"C:\scan_data\logs\ow.log",
    )
    assert "E-101" in text
    assert "Sensor self-check failed" in text
    assert "One or more devices did not respond." in text
    assert "2026-06-15 23:00:00" in text
    assert "1.2.3" in text
    assert "ABC123" in text
    assert r"C:\scan_data\logs\ow.log" in text


def test_smtp_config_complete_true_when_all_fields_present():
    cfg = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "u",
        "password": "p",
        "from_addr": "app@example.com",
    }
    assert bug_report.smtp_config_complete(cfg) is True


@pytest.mark.parametrize("cfg", [
    None,
    {},
    {"host": "smtp.example.com"},  # missing the rest
    {"host": "", "port": 587, "username": "u", "password": "p",
     "from_addr": "a@b.c"},  # empty host
])
def test_smtp_config_incomplete_returns_false(cfg):
    assert bug_report.smtp_config_complete(cfg) is False


def test_build_mailto_url_encodes_subject_and_body():
    url = bug_report.build_mailto_url(
        "support@openwater.health",
        subject="BloodFlow Bug Report — E-101",
        body="line one\nline two & more",
    )
    split = urlsplit(url)
    assert split.scheme == "mailto"
    assert split.path == "support@openwater.health"
    q = parse_qs(split.query)
    assert q["subject"] == ["BloodFlow Bug Report — E-101"]
    assert q["body"] == ["line one\nline two & more"]
