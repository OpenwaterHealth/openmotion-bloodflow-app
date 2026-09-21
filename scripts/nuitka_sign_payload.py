"""Nuitka user plugin: Authenticode-sign the app's own binaries inside the
onefile payload (#579).

    python -m nuitka ... --user-plugin=scripts/nuitka_sign_payload.py

The onefile exe is signed after the build (scripts/package_artifacts.ps1), but
its payload is compressed inside it, so what the bootstrap extracts to
``%TEMP%\\onefile_<pid>_<time>_<random>\\`` at every launch was unsigned. On
1.5.3-rc.1 Microsoft Defender's ML model quarantined that extracted
``main.dll`` (``Program:Win32/Contebrew.A!ml``): an unsigned DLL written to
%TEMP% by a process that then loads it is what a dropper looks like.

Nuitka calls ``onStandaloneDistributionFinished`` after the standalone folder
is complete (version resources applied, DLLs copied) and before it is packed
into the onefile exe; it signs macOS bundles at the same point. Signing here
puts the signature inside the payload, so the extracted files carry it.

Only what Nuitka compiled from this app is signed. On Nuitka 4.x that is one
file, ``main.dll``: the program is built as a DLL that the onefile bootstrap
loads directly, there is no inner exe (a Nuitka that emits ``main.exe`` next to
it would get that signed too, at one more signing per build, which the build
log shows). Signings are metered (docs/SIGNING.md), and the rest of the
payload is third-party code that is either signed by its publisher already
(python3xx.dll, Qt) or not ours to vouch for.

Signing goes through installer/sign.ps1 like everything else, so with
CODESIGN_THUMBPRINT unset (every local build, dev tags, branch pushes) this is
a no-op that says so.
"""

import os
import subprocess

from nuitka.plugins.PluginBase import NuitkaPluginBase

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SIGN_SCRIPT = os.path.join(_REPO_ROOT, "installer", "sign.ps1")
_OWN_SUFFIXES = (".exe", ".dll")


def own_binaries(dist_dir, standalone_binary):
    """The binaries in ``dist_dir`` that Nuitka compiled from this app: the
    standalone launcher and any sibling with the same stem (``main.exe`` +
    ``main.dll`` on Nuitka 4.x). Top level only; never a third-party DLL."""
    stem = os.path.splitext(os.path.basename(standalone_binary))[0].lower()
    found = []
    for name in sorted(os.listdir(dist_dir)):
        base, suffix = os.path.splitext(name)
        if base.lower() == stem and suffix.lower() in _OWN_SUFFIXES:
            found.append(os.path.join(dist_dir, name))
    return found


class NuitkaPluginSignPayload(NuitkaPluginBase):
    plugin_name = "sign-payload"
    plugin_desc = "Authenticode-sign the app's own binaries before onefile packing (#579)."

    def __init__(self):
        self._dist_dir = None

    def onStandaloneDistributionFinished(self, dist_dir):
        # The binary's path arrives in the next hook; both fire back to back,
        # after the dist folder is final and before onefile packing.
        self._dist_dir = dist_dir

    def onStandaloneBinary(self, filename):
        dist_dir = self._dist_dir or os.path.dirname(filename)
        files = own_binaries(dist_dir, filename)
        if not files:
            self.sysexit("sign-payload: no app binaries found in '%s'" % dist_dir)
        if not os.environ.get("CODESIGN_THUMBPRINT"):
            self.info(
                "CODESIGN_THUMBPRINT not set, payload left unsigned: %s"
                % ", ".join(os.path.basename(f) for f in files)
            )
            return
        self.info("signing payload: %s" % ", ".join(os.path.basename(f) for f in files))
        # One call per file: `powershell -File` hands -Files a single literal
        # string, never an array.
        for path in files:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", _SIGN_SCRIPT, "-Files", path],
                check=False,
            )
            if result.returncode != 0:
                self.sysexit(
                    "sign-payload: installer/sign.ps1 failed for '%s' (exit %d)"
                    % (path, result.returncode)
                )
