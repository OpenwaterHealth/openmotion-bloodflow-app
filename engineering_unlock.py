"""Engineering-mode unlock for Research builds and the clinical service tool.

Compiled out of clinical builds (#706), the way ``app_updater`` is (#543):
``motion_connector`` imports this module only when the compiled
``CLINICAL_MODE`` is False or ``SERVICE_BUILD`` is True, and
``openwater.spec`` / ``scripts/build_nuitka.ps1`` leave it (and
``components/EngineeringUnlockModal.qml``) out of a clinical bundle. A
clinical executable therefore carries neither the engineering password nor
any code that turns engineering mode on: the connector's
``unlockEngineeringMode`` slot refuses every call there, and ``setConfig``
/ ``saveConfigs`` refuse ``engineeringMode = True`` in every build.

Research builds keep the plaintext engineering password: Research is the
engineering build, and the password only keeps a casual user out of the
engineering card. This module is the only place the literal is defined;
the check runs in Python, so it never ships in readable QML. Engineering
mode is a SESSION key: it ends at relaunch or on "Disable engineering
mode".

The entry point takes the connector so it can set the flag, emit its
signal and write its audit log; nothing here imports the connector back.
"""

from __future__ import annotations

import hmac
import logging

logger = logging.getLogger("openmotion.bloodflow-app.engineering_unlock")

_ENGINEERING_PASSWORD = "OpenwaterHealth"


def password_matches(password) -> bool:
    """True iff ``password`` is the engineering password."""
    return isinstance(password, str) and hmac.compare_digest(
        password.encode("utf-8"), _ENGINEERING_PASSWORD.encode("utf-8"))


def unlock(connector, password) -> bool:
    """Turn engineering mode on for this session if ``password`` is the
    engineering password. Audits success and failure. Returns True when
    engineering mode is on afterwards."""
    if not password_matches(password):
        logger.info("Engineering unlock attempt failed")
        connector._audit.log("engineering_unlock_failed", {})
        return False
    logger.warning("Engineering mode unlocked")
    connector._audit.log("engineering_mode_unlocked", {})
    if connector._app_config.get("engineeringMode") is not True:
        connector._app_config["engineeringMode"] = True
        connector.appConfigChanged.emit()
        connector._refresh_update_checks()
    return True
