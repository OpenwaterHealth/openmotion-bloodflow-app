"""In-app self-updater for Research builds: GitHub release check, bundle
download, signature verification and the detached install handoff.

Compiled out of clinical builds (#543, tracker M-02): ``motion_connector``
imports this module only when the compiled ``CLINICAL_MODE`` constant is
False, and ``openwater.spec`` excludes it from the clinical bundle, so a
clinical executable neither ships nor loads any of this code. The connector
keeps the ``checkForUpdates`` / ``applyUpdate`` slots and the update signals
in every variant so the QML contract is unchanged; in a clinical build they
are no-ops.

Signature policy (#544, tracker M-03, M-10, V-03, V-10, V-11): a downloaded
bundle is installed only if Windows' ``WinVerifyTrust`` reports a valid,
chained Authenticode signature **and** the signer certificate's SHA-256
thumbprint is one of ``ACCEPTED_SIGNER_SHA256``. There is no unsigned
transition path any more, and the check is a direct Win32 call: nothing is
built into a PowerShell command line from the download path.

The two entry points take the connector so they can read its config, the
beta-channel decision and emit its signals; nothing here imports the
connector back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from typing import NamedTuple, Optional

from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt

from utils import app_paths
from utils.frozen import executable_path

logger = logging.getLogger("openmotion.bloodflow-app.updater")

GITHUB_REPO = "OpenwaterHealth/openmotion-bloodflow-app"

# Publisher pin (#544). SHA-256 over the DER encoding of the signer's leaf
# certificate, as Windows shows under "Thumbprint" only for SHA-1; the SHA-256
# form is what the deferred startup self-check (#549) pins too. Rotation:
# append the successor certificate's thumbprint here and ship that release
# signed with the OLD certificate; only then may the new one sign a release.
#
#   Open Water Internet, Inc. (San Francisco, CA; serial 5995870)
#   issued by SSL.com EV Code Signing Intermediate CA RSA R3
#   valid 2026-08-05 .. 2027-11-06, SHA-1 ADFCD7CB6A7900A204F91EDF9E5ADADB5A8C1AB4
ACCEPTED_SIGNER_SHA256 = frozenset({
    "BE77B247E7776240EC65DC854C86423C4C15C00F22365385BE101189FC71AC1F",
})

# Fixed local names for the downloaded bundle. The URL's last path segment is
# never used as a filename: it comes from a network response and would end
# up inside the PowerShell helper script this module writes (V-10).
BUNDLE_FILENAME = {
    True: "Open-Motion-Research-Setup.exe",
    False: "Open-Motion-Setup.exe",
}


class SignatureInfo(NamedTuple):
    """Outcome of an Authenticode check.

    ``status`` is "Valid" for a verified, chained signature; "NotSigned" when
    the file carries none; one of a few named failures ("HashMismatch",
    "UntrustedRoot", "Expired", "Distrusted", "NotPE"); "Error" when the
    check itself could not run (non-Windows, missing DLL, exception); or the
    raw HRESULT as hex for anything else. ``thumbprint_sha256`` and
    ``subject`` are only set for "Valid".
    """
    status: str
    thumbprint_sha256: Optional[str] = None
    subject: Optional[str] = None
    detail: str = ""


# --- WinVerifyTrust (Windows only) --------------------------------------------

_HRESULT_NAMES = {
    0x800B0100: "NotSigned",       # TRUST_E_NOSIGNATURE
    0x80096010: "HashMismatch",    # TRUST_E_BAD_DIGEST
    0x800B0003: "NotPE",           # TRUST_E_SUBJECT_FORM_UNKNOWN
    0x800B0109: "UntrustedRoot",   # CERT_E_UNTRUSTEDROOT
    0x800B0101: "Expired",         # CERT_E_EXPIRED
    0x800B0111: "Distrusted",      # TRUST_E_EXPLICIT_DISTRUST
    0x800B010A: "ChainError",      # CERT_E_CHAINING
    0x80092026: "PolicyBlocked",   # CRYPT_E_SECURITY_SETTINGS
}

_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_CACHE_ONLY_URL_RETRIEVAL = 0x1000   # never stall an offline host
_CERT_NAME_SIMPLE_DISPLAY_TYPE = 4


def _win32():
    """ctypes bindings for wintrust / crypt32, built lazily (Windows only)."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD),
                    ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE),
                    ("pgKnownSubject", ctypes.POINTER(GUID))]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD),
                    ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p),
                    ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD),
                    ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
                    ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE),
                    ("pwszURLReference", wintypes.LPWSTR),
                    ("dwProvFlags", wintypes.DWORD),
                    ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    class CERT_CONTEXT(ctypes.Structure):
        _fields_ = [("dwCertEncodingType", wintypes.DWORD),
                    ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
                    ("cbCertEncoded", wintypes.DWORD),
                    ("pCertInfo", ctypes.c_void_p),
                    ("hCertStore", ctypes.c_void_p)]

    class CRYPT_PROVIDER_CERT(ctypes.Structure):
        # Only the leading fields are needed; the struct is read, never built.
        _fields_ = [("cbStruct", wintypes.DWORD),
                    ("pCert", ctypes.POINTER(CERT_CONTEXT))]

    wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

    wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.c_void_p]
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
    wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    wintrust.WTHelperGetProvSignerFromChain.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.c_void_p
    wintrust.WTHelperGetProvCertFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    wintrust.WTHelperGetProvCertFromChain.restype = ctypes.POINTER(CRYPT_PROVIDER_CERT)
    crypt32.CertGetNameStringW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.LPWSTR, wintypes.DWORD]
    crypt32.CertGetNameStringW.restype = wintypes.DWORD

    action = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                  (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    return ctypes, wintypes, wintrust, crypt32, WINTRUST_FILE_INFO, WINTRUST_DATA, action


def _verify_with_wintrust(path: str) -> tuple[int, Optional[str], Optional[str]]:
    """Run WinVerifyTrust on ``path``.

    Returns ``(hresult, signer_sha256, signer_subject)``; the last two are
    None unless the result is 0. Separated from :func:`verify_authenticode`
    so tests can substitute it without a signed fixture.
    """
    (ctypes, wintypes, wintrust, crypt32,
     WINTRUST_FILE_INFO, WINTRUST_DATA, action) = _win32()

    file_info = WINTRUST_FILE_INFO()
    file_info.cbStruct = ctypes.sizeof(file_info)
    file_info.pcwszFilePath = os.fspath(path)
    file_info.hFile = None
    data = WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(data)
    data.dwUIChoice = _WTD_UI_NONE
    data.fdwRevocationChecks = _WTD_REVOKE_NONE
    data.dwUnionChoice = _WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = _WTD_STATEACTION_VERIFY
    data.dwProvFlags = _WTD_CACHE_ONLY_URL_RETRIEVAL
    no_window = wintypes.HWND(-1)   # INVALID_HANDLE_VALUE: never show UI

    sha256 = subject = None
    hresult = wintrust.WinVerifyTrust(no_window, ctypes.byref(action), ctypes.byref(data))
    try:
        if hresult == 0:
            prov = wintrust.WTHelperProvDataFromStateData(data.hWVTStateData)
            sgnr = wintrust.WTHelperGetProvSignerFromChain(prov, 0, False, 0) if prov else None
            cert = wintrust.WTHelperGetProvCertFromChain(sgnr, 0) if sgnr else None
            if cert and cert.contents.pCert:
                ctx = cert.contents.pCert.contents
                der = ctypes.string_at(ctx.pbCertEncoded, ctx.cbCertEncoded)
                sha256 = hashlib.sha256(der).hexdigest().upper()
                buf = ctypes.create_unicode_buffer(512)
                n = crypt32.CertGetNameStringW(
                    cert.contents.pCert, _CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, buf, 512)
                subject = buf.value if n > 1 else None
    finally:
        data.dwStateAction = _WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(no_window, ctypes.byref(action), ctypes.byref(data))
    return hresult & 0xFFFFFFFF, sha256, subject


def verify_authenticode(path: str) -> SignatureInfo:
    """Verify the Authenticode signature of ``path`` and identify its signer.

    A valid result means Windows accepted the signature and chained it to a
    trusted root (cached revocation data only, so an offline host does not
    stall; the timestamp counter-signature keeps an expired certificate
    valid for builds signed while it was current).
    """
    if sys.platform != "win32":
        return SignatureInfo("Error", detail="Authenticode verification needs Windows")
    try:
        hresult, sha256, subject = _verify_with_wintrust(path)
    except Exception as e:                       # missing DLL, bad handle, ...
        logger.warning("Authenticode check could not run for %s: %s", path, e)
        return SignatureInfo("Error", detail=str(e))
    if hresult == 0:
        if not sha256:
            return SignatureInfo("Error", detail="signature valid but signer certificate unreadable")
        return SignatureInfo("Valid", sha256, subject)
    return SignatureInfo(_HRESULT_NAMES.get(hresult, f"0x{hresult:08X}"))


# --- policy -------------------------------------------------------------------

def _update_decision(info: SignatureInfo, accepted=ACCEPTED_SIGNER_SHA256):
    """Decide whether to launch the downloaded bundle.

    Returns (should_launch: bool, error_message: str | None). Only a valid
    signature from a pinned Openwater certificate launches; there is no
    unsigned path (#544).
    """
    if info.status == "Valid":
        if info.thumbprint_sha256 in accepted:
            return True, None
        who = info.subject or "unknown"
        return False, (f"Update is signed by an unexpected publisher ({who}); "
                       "refusing to install.")
    if info.status == "NotSigned":
        return False, ("Update bundle is not signed; refusing to install. "
                       "Pre-release builds are unsigned: install them from the "
                       "releases page instead.")
    if info.status == "Error":
        return False, f"Update signature could not be verified: {info.detail or 'check failed'}"
    return False, f"Update signature check failed: {info.status}"


def _select_update_asset(assets: list, is_research: bool):
    """Return download URL of the Setup bundle matching the running variant.

    Research builds match ``Open-Motion-Research-Setup-*.exe``; clinical
    builds match ``Open-Motion-Setup-*.exe``. Variant detection is a
    case-insensitive "research" substring check so the pre-1.4.0 asset
    names (``Openwater-Setup-*[_Research].exe``) still match. Returns
    None if no match.
    """
    for asset in assets:
        name = (asset.get("name") or "")
        low = name.lower()
        if not low.endswith(".exe"):
            continue
        asset_is_research = "research" in low
        if asset_is_research == is_research:
            return asset.get("browser_download_url")
    return None


def _select_release(releases, include_prerelease):
    """Pick the newest published release from a GitHub /releases list.

    GitHub returns releases newest-first. Drafts are never installable and are
    always skipped. When include_prerelease is False, prereleases are skipped
    too. Returns the chosen release dict, or None if nothing is eligible (#386)."""
    for rel in releases or []:
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not include_prerelease:
            continue
        return rel
    return None


def _is_bundle_url(url) -> bool:
    """True if ``url`` is a Setup .exe bundle, not the release HTML page.

    Guards against the GitHub ``html_url`` fallback: without this, a release
    with no matching installer asset would make the updater download an HTML
    page and try to execute it.
    """
    return isinstance(url, str) and url.lower().endswith(".exe")


def _looks_like_pe(head: bytes) -> bool:
    """True if the bytes start with the 'MZ' signature of a Windows executable.

    A truncated download or an HTML error page will not start with 'MZ', so
    this rejects corrupt downloads before they are launched.
    """
    return head[:2] == b"MZ"


def _build_update_helper_script(
    app_pid: int, installer: str, app_exe: str
) -> str:
    """Return a PowerShell script that performs the in-place upgrade handoff.

    The running app cannot upgrade its own files while it holds them, and the
    Burn bundle does not relaunch the app. So the app spawns this detached
    helper and then exits; the helper (1) waits for the app to fully exit to
    avoid a FilesInUse conflict — and force-kills it if it doesn't exit in
    time, since a wedged GUI thread (e.g. a synchronous SDK/USB call blocking
    the event loop) can otherwise leave the app alive and stall the whole
    upgrade, (2) runs the bundle non-interactively (one UAC elevation, no
    bootstrapper UI), then (3) relaunches the app.

    Both paths are ours (a fixed name under the app's data directory and
    the launched executable path), never derived from network input; the guard below
    makes that a hard rule rather than a convention.
    """
    for p in (installer, app_exe):
        if any(ch in p for ch in "\"`$\r\n"):
            raise ValueError(f"unsafe character in helper path: {p!r}")
    return (
        "# OpenWater in-app update helper (auto-generated; do not edit)\n"
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"# 1. Wait for the running app (PID {app_pid}) to exit so the\n"
        "#    installer can replace its files without a FilesInUse conflict.\n"
        "#    If it doesn't exit in time (the app may not self-exit if the\n"
        "#    GUI thread is wedged), force-kill it - installing over a live app fails the\n"
        "#    in-place file swap.\n"
        "$deadline = (Get-Date).AddSeconds(10)\n"
        f"while (Get-Process -Id {int(app_pid)} -ErrorAction SilentlyContinue) {{\n"
        "    if ((Get-Date) -gt $deadline) {\n"
        f"        Stop-Process -Id {int(app_pid)} -Force -ErrorAction SilentlyContinue\n"
        "        Start-Sleep -Milliseconds 500\n"
        "        break\n"
        "    }\n"
        "    Start-Sleep -Milliseconds 250\n"
        "}\n"
        "# 2. Run the upgrade non-interactively (one UAC elevation).\n"
        f"Start-Process -FilePath \"{installer}\" "
        "-ArgumentList '/passive','/norestart' -Wait\n"
        "# 3. Relaunch the (now-updated) app.\n"
        f"Start-Process -FilePath \"{app_exe}\"\n"
    )


def version_newer(remote: str, local: str) -> bool:
    """Return True if remote version is strictly newer than local.

    Handles versions like '0.4.3', 'pre-0.4.3', '1.0-pre3', and
    setuptools_scm-style dev strings like '1.2.0-dev.1-0-gabc1234-dirty'.
    Compares the leading dotted-numeric core; any suffix after it
    marks a pre-release, which counts as older than the same base
    version. (The old int() parse raised on dev suffixes and fell
    back to [0], making every remote release look newer.)
    """
    def parse(v):
        is_pre = v.startswith("pre-")
        base = v[4:] if is_pre else v
        m = re.match(r"(\d+(?:\.\d+)*)(.*)", base)
        if not m:
            return [0], is_pre
        parts = [int(x) for x in m.group(1).split(".")]
        if m.group(2):
            is_pre = True
        return parts, is_pre

    r_parts, r_pre = parse(remote)
    l_parts, l_pre = parse(local)

    if r_parts != l_parts:
        return r_parts > l_parts
    # Same numeric base.
    if l_pre and not r_pre:
        return True   # local prerelease, remote full -> remote is newer
    if r_pre and l_pre:
        # Both prereleases of the same base (rc.1 -> rc.2, dev.0 -> dev.1,
        # dev -> rc): order by PEP 440 prerelease precedence. Only the beta
        # channel ever compares pre-vs-pre (#386). packaging parses the
        # project's rc.N/dev.N tags; if it can't, keep the old not-newer
        # behavior so a weird tag never triggers a spurious "update".
        try:
            from packaging.version import Version
            return Version(remote) > Version(local)
        except Exception:
            return False
    return False


def check_for_updates(connector) -> None:
    """Query GitHub for a newer release and emit the connector's update
    signals. Runs on the connector's background thread; the connector has
    already refused the call for a clinical config."""
    import urllib.request
    from version import get_version

    cfg = connector._app_config
    # ``updateRepo`` points the check at a staging/mirror repo; the broader
    # ``updateApiUrl`` fully overrides the single-release endpoint (used by
    # the local fake-releases server for offline end-to-end update testing).
    # Both absent => the production GitHub repo. On the beta channel we hit
    # the /releases LIST endpoint and pick the newest published (incl.
    # prerelease); otherwise the stable /releases/latest single object (#386).
    repo = cfg.get("updateRepo") or GITHUB_REPO
    beta = connector._beta_enabled()
    override = cfg.get("updateApiUrl")
    if override:
        api_url = override.replace("/releases/latest", "/releases") if beta else override
    else:
        base = f"https://api.github.com/repos/{repo}/releases"
        api_url = base if beta else base + "/latest"
    try:
        req = urllib.request.Request(
            api_url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        # Select by RESPONSE SHAPE, not the beta flag: the beta channel hits
        # the /releases LIST endpoint (newest-first) -> pick newest non-draft;
        # stable /releases/latest returns one object. An updateApiUrl override
        # may return a single object even on beta, so handle both (#386).
        release = (_select_release(data, include_prerelease=True)
                   if isinstance(data, list) else data)
        if not release:
            connector.updateNotAvailable.emit()
            return

        remote_tag = release.get("tag_name", "").lstrip("v")
        if not remote_tag:
            connector.updateCheckFailed.emit("Could not determine latest release tag.")
            return

        # Find the Setup bundle matching this build's variant.
        # clinicalMode True = clinical build; False = Research/full build.
        is_research = not bool(cfg.get("clinicalMode", False))
        download_url = _select_update_asset(release.get("assets", []), is_research)

        # Strip local metadata for comparison (e.g. "+3.gabc1234.dirty").
        local_base = get_version().split("+")[0]

        if not version_newer(remote_tag, local_base):
            logger.info("App is up to date (%s >= %s)", local_base, remote_tag)
            connector.updateNotAvailable.emit()
        elif not _is_bundle_url(download_url):
            # Newer release exists but has no matching installer asset
            # (e.g. assets still uploading) -- don't offer an in-place
            # update we can't actually perform.
            logger.warning(
                "Update %s available but no installer asset found", remote_tag)
            connector.updateNotAvailable.emit()
        else:
            logger.info(
                "Update available: %s (current: %s)", remote_tag, local_base)
            connector.updateAvailable.emit(remote_tag, download_url)

    except Exception as e:
        logger.warning("Update check failed: %s", e)
        connector.updateCheckFailed.emit(str(e))


def apply_update(connector, download_url: str) -> bool:
    """Download the bundle, verify its signature against the publisher pin,
    spawn the detached install helper and ask Qt to quit. Runs on the
    connector's background thread; the connector owns the re-entry flag
    around this call. Returns True once the install is handed off (the app
    is quitting), False on any failure (already reported through
    ``updateCheckFailed``)."""
    import urllib.request
    import subprocess

    cfg = connector._app_config
    try:
        if not _is_bundle_url(download_url):
            connector.updateCheckFailed.emit("No installer for this update.")
            return False

        portable = bool(cfg.get("portableMode", False))
        updates_dir = app_paths.writable_root(portable) / app_paths.DATA_DIRNAME / "updates"
        updates_dir.mkdir(parents=True, exist_ok=True)
        # Clear stale downloads so old installers don't accumulate.
        for stale in updates_dir.glob("*"):
            try:
                stale.unlink()
            except OSError:
                pass

        is_research = not bool(cfg.get("clinicalMode", False))
        dest = updates_dir / BUNDLE_FILENAME[is_research]
        connector.updateProgress.emit("Downloading update…")
        logger.info("Downloading update %s -> %s", download_url, dest)
        urllib.request.urlretrieve(download_url, str(dest))

        # Reject truncated / non-executable downloads before launching.
        with open(dest, "rb") as f:
            head = f.read(2)
        if not _looks_like_pe(head):
            connector.updateCheckFailed.emit(
                "Downloaded update is not a valid installer."
            )
            return False

        info = verify_authenticode(str(dest))
        should_launch, error = _update_decision(info)
        logger.info("Update bundle signature: %s signer=%s sha256=%s -> %s",
                    info.status, info.subject, info.thumbprint_sha256,
                    "install" if should_launch else "refuse")
        if not should_launch:
            connector.updateCheckFailed.emit(error)
            return False

        # The app cannot replace its own running files, and the Burn
        # bundle does not relaunch the app. So write a detached helper
        # that waits for us to exit, runs the bundle silently, then
        # relaunches the (now-updated) app. Then quit.
        helper = updates_dir / "update_helper.ps1"
        helper.write_text(
            _build_update_helper_script(
                os.getpid(), str(dest), str(executable_path())
            ),
            encoding="utf-8",
        )
        connector.updateProgress.emit("Installing update…")
        logger.info("Spawning update helper; quitting for upgrade")
        # NB: DETACHED_PROCESS leaves powershell with no console and no std
        # handles, so it dies instantly WITHOUT running the script (verified
        # in isolation — the helper never executed, which is why earlier
        # upgrades silently never installed). CREATE_NO_WINDOW gives it a
        # hidden console and DEVNULL std handles, so the detached helper
        # actually runs.
        subprocess.Popen(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(helper),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Ask Qt to shut down gracefully (aboutToQuit -> handle_exit stops
        # the hardware monitor, releases the USB transport, flushes the
        # audit log). This runs on a worker thread, so DO NOT call
        # QCoreApplication.quit() directly: a cross-thread quit() can
        # block inside the C++ call while HOLDING the GIL, which freezes
        # the whole process — every thread, including any hard-exit
        # backstop, starves (observed under the old qasync event loop via
        # py-spy; the queued post is the safe pattern under the plain Qt
        # loop too). Post the quit to the main thread instead;
        # QueuedConnection just enqueues an event and returns immediately,
        # so this worker never blocks. If graceful
        # teardown stalls or the quit is ignored, the detached helper
        # force-kills us after its grace window and the upgrade proceeds
        # regardless — so we don't rely on the app exiting itself.
        QMetaObject.invokeMethod(
            QCoreApplication.instance(),
            "quit",
            Qt.ConnectionType.QueuedConnection,
        )
        return True
    except Exception as e:
        logger.error("applyUpdate failed: %s", e)
        connector.updateCheckFailed.emit(str(e))
        return False
