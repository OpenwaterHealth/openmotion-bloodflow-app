"""Bug-report assembly and delivery for the critical-error modal.

Split into pure helpers (report text, config check, mailto URL — unit-tested)
and thin I/O wrappers (SMTP send, reveal-in-Explorer — side-effecting, exercised
manually). The connector orchestrates: build the text, then either send via SMTP
(if fully configured) or fall back to opening the user's mail client.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from email.message import EmailMessage
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

_SMTP_REQUIRED_FIELDS = ("host", "port", "username", "password", "from_addr")


# --- pure helpers -----------------------------------------------------------
def build_report_text(
    *,
    code: str,
    title: str,
    message: str,
    timestamp: str,
    app_version: str,
    device_info: str,
    log_path: str,
) -> str:
    """Human-readable report body included in the email / clipboard."""
    return (
        "OpenWater BloodFlow — Critical Error Report\n"
        "-------------------------------------------\n"
        f"Code:       {code}\n"
        f"Title:      {title}\n"
        f"Message:    {message}\n"
        f"Time:       {timestamp}\n"
        f"App version: {app_version}\n"
        f"Devices:    {device_info}\n"
        f"Log file:   {log_path}\n"
        "\n"
        "Please attach the log file above if it is not already attached.\n"
    )


def smtp_config_complete(cfg: dict | None) -> bool:
    """True iff every required SMTP field is present and non-empty."""
    if not cfg:
        return False
    return all(str(cfg.get(f, "")).strip() for f in _SMTP_REQUIRED_FIELDS)


def build_mailto_url(address: str, *, subject: str, body: str) -> str:
    """Build a ``mailto:`` URL with URL-encoded subject and body."""
    query = urlencode({"subject": subject, "body": body})
    return f"mailto:{address}?{query}"


# --- I/O wrappers (side-effecting; not unit-tested) -------------------------
def send_via_smtp(cfg: dict, *, to_addr: str, subject: str, body: str,
                  log_path: str | None) -> None:
    """Send the report via SMTP with the log attached. Raises on failure."""
    import smtplib

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    if log_path and os.path.isfile(log_path):
        with open(log_path, "rb") as fh:
            msg.add_attachment(
                fh.read(), maintype="text", subtype="plain",
                filename=os.path.basename(log_path),
            )

    with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=30) as srv:
        if cfg.get("use_tls", True):
            srv.starttls()
        srv.login(cfg["username"], cfg["password"])
        srv.send_message(msg)


def reveal_in_file_manager(path: str) -> None:
    """Open the OS file manager with ``path`` selected. Best-effort."""
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("Could not reveal log file %s: %s", path, exc)
