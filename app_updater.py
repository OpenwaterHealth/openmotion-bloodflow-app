"""In-app self-updater for Research builds: GitHub release check, bundle
download, signature check and the detached install handoff.

Compiled out of clinical builds (#543, tracker M-02): ``motion_connector``
imports this module only when the compiled ``CLINICAL_MODE`` constant is
False, and ``openwater.spec`` excludes it from the clinical bundle, so a
clinical executable neither ships nor loads any of this code. The connector
keeps the ``checkForUpdates`` / ``applyUpdate`` slots and the update signals
in every variant so the QML contract is unchanged; in a clinical build they
are no-ops.

The two entry points take the connector so they can read its config, the
beta-channel decision and emit its signals; nothing here imports the
connector back.
"""

from __future__ import annotations

import json
import logging
import os
import re

from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt

from utils import app_paths
from utils.frozen import executable_path

logger = logging.getLogger("openmotion.bloodflow-app.updater")

GITHUB_REPO = "OpenwaterHealth/openmotion-bloodflow-app"

# Flip to True once release builds are Authenticode-signed; until then an
# unsigned (NotSigned) update bundle is allowed through with a logged warning
# (#544 owns the flip and the publisher pin).
_REQUIRE_SIGNED_UPDATES = False


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


def _authenticode_status(path: str) -> str:
    """Return the Authenticode signature status of ``path``.

    Uses PowerShell's Get-AuthenticodeSignature (always present on Windows).
    Returns one of 'Valid', 'NotSigned', 'HashMismatch', 'UnknownError', ... or
    'Error' if the check itself could not run.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() or "Error"
    except Exception:
        return "Error"


def _update_decision(status: str, require_signed: bool):
    """Decide whether to launch the downloaded bundle given its signature.

    Returns (should_launch: bool, error_message: str | None).
    """
    if status == "Valid":
        return True, None
    if status == "NotSigned":
        if require_signed:
            return False, "Update is not signed; refusing to install."
        return True, None  # transition period: allow with a warning logged
    return False, f"Update signature check failed: {status}"


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
    """
    return (
        "# OpenWater in-app update helper (auto-generated; do not edit)\n"
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"# 1. Wait for the running app (PID {app_pid}) to exit so the\n"
        "#    installer can replace its files without a FilesInUse conflict.\n"
        "#    If it doesn't exit in time (the app may not self-exit if the\n"
        "#    GUI thread is wedged), force-kill it - installing over a live app fails the\n"
        "#    in-place file swap.\n"
        "$deadline = (Get-Date).AddSeconds(10)\n"
        f"while (Get-Process -Id {app_pid} -ErrorAction SilentlyContinue) {{\n"
        "    if ((Get-Date) -gt $deadline) {\n"
        f"        Stop-Process -Id {app_pid} -Force -ErrorAction SilentlyContinue\n"
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


def apply_update(connector, download_url: str) -> None:
    """Download the bundle, verify it, spawn the detached install helper and
    ask Qt to quit. Runs on the connector's background thread; the connector
    owns the re-entry flag around this call."""
    import urllib.request
    import subprocess

    cfg = connector._app_config
    try:
        if not _is_bundle_url(download_url):
            connector.updateCheckFailed.emit("No installer for this update.")
            return

        portable = bool(cfg.get("portableMode", False))
        updates_dir = app_paths.writable_root(portable) / app_paths.DATA_DIRNAME / "updates"
        updates_dir.mkdir(parents=True, exist_ok=True)
        # Clear stale downloads so old installers don't accumulate.
        for stale in updates_dir.glob("*"):
            try:
                stale.unlink()
            except OSError:
                pass

        dest = updates_dir / download_url.rsplit("/", 1)[-1]
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
            return

        status = _authenticode_status(str(dest))
        should_launch, error = _update_decision(
            status, _REQUIRE_SIGNED_UPDATES
        )
        if not should_launch:
            connector.updateCheckFailed.emit(error)
            return
        if status == "NotSigned":
            logger.warning(
                "Update bundle not signed (transition); proceeding"
            )

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
    except Exception as e:
        logger.error("applyUpdate failed: %s", e)
        connector.updateCheckFailed.emit(str(e))
