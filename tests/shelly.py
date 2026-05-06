#!/usr/bin/env python3
"""
shelly.py — Shelly WiFi outlet driver for hardware-in-the-loop tests.

Controls a Shelly relay over LAN so tests can power-cycle the device
under test (DUT). Auto-detects Gen1 (HTTP) and Gen2+ (RPC) Shelly
devices. Use this when:

  - The DUT (e.g. an OpenMOTION console) is plugged into a Shelly outlet
    on the same LAN as the test runner.
  - A test needs to verify reconnect / recovery behavior after power loss.
  - You want to reset the DUT into a known state between tests.

Hardware setup
--------------
Required on the runner / dev machine:

  - The Shelly outlet is reachable on the LAN (HTTP, port 80).
  - The outlet's IP or hostname is exported as ``$SHELLY_IP_ADDRESS``.
  - The DUT is the load wired into that outlet.

Optional environment variables:

  - ``$SHELLY_RELAY``    — relay index for multi-channel devices (default 0)
  - ``$SHELLY_USER``     — HTTP auth username, if the outlet enforces it
  - ``$SHELLY_PASSWORD`` — HTTP auth password (paired with $SHELLY_USER)

CLI
---
::

    python shelly.py                       # default: cycle (off 5s, on)
    python shelly.py toggle
    python shelly.py on
    python shelly.py off
    python shelly.py status
    python shelly.py cycle --off-time 3.0

    # Override host (otherwise uses $SHELLY_IP_ADDRESS)
    python shelly.py --host 192.168.1.42 toggle
    python shelly.py --host shelly-plug-s.local --relay 0 toggle

Library — quickest path
-----------------------
::

    from shelly import on, off, toggle, power_cycle, is_on

    on()                              # turn the DUT's outlet on
    off()                             # turn it off
    toggle()                          # invert state, return new state
    power_cycle(off_time=5.0)         # off → wait 5s → on
    if is_on(): ...

These module-level functions all share one ``ShellyOutlet`` constructed
lazily from ``$SHELLY_IP_ADDRESS`` on first call. A missing env var
raises ``RuntimeError`` — tests should ``pytest.skip(...)`` rather than
fail in that case. To target a different host inside one process, use
the class directly::

    from shelly import ShellyOutlet
    outlet = ShellyOutlet("192.168.1.42")
    outlet.power_cycle(off_time=2.0)

Pytest patterns
---------------
A fixture that skips cleanly when the outlet is unreachable and leaves
the DUT powered ON at teardown so downstream tests are usable::

    import pytest, shelly

    @pytest.fixture(scope="module")
    def outlet():
        try:
            out = shelly.default_outlet()
            out.is_on()                  # one round-trip to confirm reachable
        except Exception as e:
            pytest.skip(f"Shelly outlet not reachable: {e}")
        yield out
        try: out.on()
        except Exception: pass

Verifying the *app* reconnected
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The outlet only controls power. It does not know whether the DUT booted,
or whether the host-side app reconnected over USB. Pair every power
action with an app-side observation — for example, tail the bloodflow
app log for ``state ... -> CONNECTED`` lines. See
``test_connection_redesign.py`` in this directory for a complete
end-to-end example (snapshot log size → power action → wait for pattern).

Return-value contract
---------------------
- ``on()`` / ``off()`` / ``set(on)`` re-read the relay after writing and
  return ``True`` only if the post-write state matches the request.
  ``False`` means the outlet did not actually change state — treat as a
  hardware/network problem, not a test failure.
- ``toggle()`` returns the **new** state (``True`` == on).
- ``is_on()`` returns the **current** state from a fresh read.
- ``power_cycle(off_time, settle_time)`` returns the post-cycle state
  (``is_on()`` after ``settle_time`` has elapsed). ``True`` means the
  cycle completed successfully.

Failure modes & gotchas
-----------------------
- **Missing env var**: ``RuntimeError`` from ``from_env`` / module-level
  helpers when ``$SHELLY_IP_ADDRESS`` is unset. Skip, do not fail.
- **Network errors**: ``requests.Timeout`` / ``requests.ConnectionError``
  surface when the outlet is unreachable. ``_get`` retries once with a
  0.5 s backoff to ride through transient hiccups; persistent loss
  re-raises.
- **Relay duty-cycle limits**: rapid sub-second on/off in a tight loop
  can trip a Shelly device's own protection — it stops answering HTTP
  for several seconds and may even reboot. **Hold each on/off phase
  ≥ 2 s** when stress-testing reconnect logic, otherwise the test
  exercises the relay's firmware rather than the DUT.
- **Singleton outlet**: ``default_outlet()`` returns a process-wide
  cached instance. Concurrent tests on one runner contend for the same
  physical relay; serialize them (``pytest-xdist --dist loadfile`` or
  appropriate fixture scope) — there is no locking.
- **Mechanical relay**: the outlet physically clicks each toggle.
  Excessive cycling shortens its life. Prefer fewer, more meaningful
  power events over thousand-cycle stress runs.

Generation compatibility
------------------------
The first call to a fresh outlet hits ``/shelly`` to detect generation,
caches the answer, then routes subsequent calls accordingly:

  - Gen1 (older Plug / Plug S, firmware ≤ 1.x):
    ``GET /relay/<id>?turn=<on|off|toggle>``
  - Gen2+ (Plus / Pro):
    ``GET /rpc/Switch.Set?id=<id>&on=<true|false>``

Authenticated outlets pass through ``requests`` basic auth.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

import requests

ENV_HOST = "SHELLY_IP_ADDRESS"
ENV_RELAY = "SHELLY_RELAY"
ENV_USER = "SHELLY_USER"
ENV_PASSWORD = "SHELLY_PASSWORD"


class ShellyOutlet:
    """Minimal Shelly relay client.

    One instance controls one relay channel on one device. Cheap to
    construct; first call to any action incurs a generation-detection
    round-trip (``/shelly``).

    Parameters
    ----------
    host:
        IP address or mDNS hostname (e.g. ``"192.168.1.42"`` or
        ``"shelly-plug-s.local"``). Required.
    relay:
        Relay/switch index for multi-channel devices. Single-relay
        outlets only have index 0.
    timeout:
        HTTP timeout in seconds for each request. ``_get`` retries once
        on timeout, so the worst-case wait is ``2 * timeout + 0.5 s``.
    auth:
        Optional ``(username, password)`` for outlets with HTTP auth
        enabled.
    """

    def __init__(
        self,
        host: str,
        relay: int = 0,
        timeout: float = 3.0,
        auth: Optional[tuple[str, str]] = None,
    ) -> None:
        if not host:
            raise ValueError(
                f"Shelly host is required (pass host= or set ${ENV_HOST})."
            )
        self.host = host
        self.relay = relay
        self.timeout = timeout
        self.auth = auth
        self._gen: Optional[int] = None

    @classmethod
    def from_env(cls, timeout: float = 3.0) -> "ShellyOutlet":
        """Construct an outlet from environment variables.

        Reads ``$SHELLY_IP_ADDRESS`` (required), ``$SHELLY_RELAY`` (default 0),
        and ``$SHELLY_USER`` / ``$SHELLY_PASSWORD`` (optional).
        """
        host = os.environ.get(ENV_HOST)
        if not host:
            raise RuntimeError(
                f"${ENV_HOST} is not set. Export the Shelly outlet's IP or "
                f"hostname before running tests that power-cycle the device."
            )
        relay = int(os.environ.get(ENV_RELAY, "0"))
        user = os.environ.get(ENV_USER)
        password = os.environ.get(ENV_PASSWORD)
        auth = (user, password) if user and password else None
        return cls(host, relay=relay, timeout=timeout, auth=auth)

    # ------------------------------------------------------------------ internals
    def _get(self, path: str) -> dict:
        """GET ``path`` from the outlet with one retry on transient HTTP errors.

        Shelly devices occasionally drop a request after rapid relay
        actuation. One retry with a short backoff keeps tests resilient
        without masking a genuinely-down outlet.
        """
        url = f"http://{self.host}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                r = requests.get(url, timeout=self.timeout, auth=self.auth)
                r.raise_for_status()
                return r.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                if attempt == 0:
                    time.sleep(0.5)
        raise last_exc  # type: ignore[misc]

    def _detect_gen(self) -> int:
        if self._gen is not None:
            return self._gen
        info = self._get("/shelly")
        # Gen2+ reports {"gen": 2, ...}; Gen1 lacks the field.
        self._gen = int(info.get("gen", 1))
        return self._gen

    # ------------------------------------------------------------------ actions
    def is_on(self) -> bool:
        """Return the relay's current on/off state from a fresh read."""
        gen = self._detect_gen()
        if gen >= 2:
            status = self._get(f"/rpc/Switch.GetStatus?id={self.relay}")
            return bool(status.get("output", False))
        status = self._get(f"/relay/{self.relay}")
        return bool(status.get("ison", False))

    def set(self, on: bool) -> bool:
        """Drive the relay to ``on`` and confirm by reading back.

        Returns ``True`` only if the post-write state matches the
        requested state. ``False`` means the outlet did not actually
        change state — typically a network or device problem.
        """
        gen = self._detect_gen()
        if gen >= 2:
            resp = self._get(
                f"/rpc/Switch.Set?id={self.relay}&on={'true' if on else 'false'}"
            )
            # Gen2 returns {"was_on": bool}; re-read to confirm.
            return self.is_on() == on
        resp = self._get(f"/relay/{self.relay}?turn={'on' if on else 'off'}")
        return bool(resp.get("ison", False)) == on

    def on(self) -> bool:
        """Turn the relay on. Returns ``True`` if the outlet is now on."""
        return self.set(True)

    def off(self) -> bool:
        """Turn the relay off. Returns ``True`` if the outlet is now off."""
        return self.set(False)

    def toggle(self) -> bool:
        """Invert the current state. Returns the **new** state (True == on)."""
        gen = self._detect_gen()
        if gen >= 2:
            self._get(f"/rpc/Switch.Toggle?id={self.relay}")
            return self.is_on()
        resp = self._get(f"/relay/{self.relay}?turn=toggle")
        return bool(resp.get("ison", False))

    def power_cycle(self, off_time: float = 2.0, settle_time: float = 0.5) -> bool:
        """Off → wait ``off_time`` → on → wait ``settle_time``.

        Returns ``True`` if the outlet is on after the cycle (i.e. the
        cycle completed). Picking ``off_time`` for hardware reconnect
        tests: ≥ 3 s gives USB enumeration time to clean up and the
        host OS time to drop the device; ≤ 1 s often does not.
        """
        self.off()
        time.sleep(off_time)
        self.on()
        time.sleep(settle_time)
        return self.is_on()


# ----------------------------------------------- Module-level test helpers ---
# Convenience wrappers for test scripts. All five forward to one shared
# ``ShellyOutlet`` constructed lazily from $SHELLY_IP_ADDRESS on the first
# call, so tests can ``from shelly import toggle, power_cycle`` and start
# using them with zero setup. The shared instance is a process-wide
# singleton — see "Failure modes" in the module docstring.

_default_outlet: Optional[ShellyOutlet] = None


def default_outlet() -> ShellyOutlet:
    """Return the process-wide shared outlet, building it on first call.

    Reads ``$SHELLY_IP_ADDRESS`` (and the optional auth/relay env vars)
    on first use. Raises ``RuntimeError`` if the host is not configured —
    callers in pytest should ``pytest.skip(...)`` rather than fail.
    """
    global _default_outlet
    if _default_outlet is None:
        _default_outlet = ShellyOutlet.from_env()
    return _default_outlet


def is_on() -> bool:
    """Return the shared outlet's current on/off state (fresh read)."""
    return default_outlet().is_on()


def on() -> bool:
    """Turn the shared outlet on; returns ``True`` on success."""
    return default_outlet().on()


def off() -> bool:
    """Turn the shared outlet off; returns ``True`` on success."""
    return default_outlet().off()


def toggle() -> bool:
    """Toggle the shared outlet; returns the **new** state."""
    return default_outlet().toggle()


def power_cycle(off_time: float = 2.0, settle_time: float = 0.5) -> bool:
    """Power-cycle the shared outlet (off → wait → on → settle).

    Defaults are intentionally short for unit-style use. For
    USB-reconnect tests against an OpenMOTION device, prefer
    ``off_time=5.0`` so the OS fully drops the device.
    """
    return default_outlet().power_cycle(off_time=off_time, settle_time=settle_time)


# --------------------------------------------------------------------------- CLI
def parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control a Shelly WiFi outlet (toggle/on/off/status/cycle)."
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="cycle",
        choices=["toggle", "on", "off", "status", "cycle"],
        help="What to do with the outlet (default: cycle).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(ENV_HOST),
        help=f"Shelly IP or mDNS hostname. Defaults to ${ENV_HOST}.",
    )
    parser.add_argument(
        "--relay",
        type=int,
        default=int(os.environ.get(ENV_RELAY, "0")),
        help="Relay/switch index (default 0).",
    )
    parser.add_argument(
        "--off-time",
        type=float,
        default=5.0,
        help="Seconds to hold off during 'cycle' (default 5.0).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="HTTP timeout in seconds (default 3.0).",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get(ENV_USER),
        help=f"Optional HTTP auth username (or ${ENV_USER}).",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get(ENV_PASSWORD),
        help=f"Optional HTTP auth password (or ${ENV_PASSWORD}).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_cli()

    if not args.host:
        print(
            f"❌  No host given. Pass --host or set ${ENV_HOST}.", file=sys.stderr
        )
        return 2

    auth = (args.user, args.password) if args.user and args.password else None
    outlet = ShellyOutlet(args.host, relay=args.relay, timeout=args.timeout, auth=auth)

    try:
        if args.action == "status":
            state = outlet.is_on()
            print(f"[{args.host}] relay {args.relay}: {'ON' if state else 'OFF'}")
            return 0

        if args.action == "on":
            ok = outlet.on()
            print(f"[{args.host}] on -> {'ON' if ok else 'FAILED'}")
            return 0 if ok else 1

        if args.action == "off":
            ok = outlet.off()
            print(f"[{args.host}] off -> {'OFF' if ok else 'FAILED'}")
            return 0 if ok else 1

        if args.action == "toggle":
            state = outlet.toggle()
            print(f"[{args.host}] toggle -> {'ON' if state else 'OFF'}")
            return 0

        if args.action == "cycle":
            print(f"[{args.host}] power-cycling (off {args.off_time:.1f}s) …")
            ok = outlet.power_cycle(off_time=args.off_time)
            print(f"[{args.host}] cycle -> {'ON' if ok else 'FAILED'}")
            return 0 if ok else 1

    except requests.RequestException as exc:
        print(f"❌  HTTP error talking to {args.host}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"❌  {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
