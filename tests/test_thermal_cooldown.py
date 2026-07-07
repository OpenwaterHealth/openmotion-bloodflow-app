"""Unit tests for thermal_cooldown (issue #102). No hardware, no Qt app."""
import math

import pytest

from thermal_cooldown import q88_to_celsius, read_camera_temps

pytestmark = pytest.mark.unit


class _FakeSensor:
    """temps_by_cam: cam -> float degC, False (fw error), or absent (raises)."""

    def __init__(self, temps_by_cam, connected=True):
        self._temps = temps_by_cam
        self._connected = connected
        self.read_calls = 0

    def is_connected(self):
        return self._connected

    def i2c_read_register(self, dev, addr, read_len, reg_addr_size, mux_channel):
        self.read_calls += 1
        assert (dev, addr, read_len, reg_addr_size) == (0x36, 0x4D2A, 2, 2)
        val = self._temps[mux_channel]          # KeyError = unreachable camera
        if val is False:
            return False
        raw = int(round(val * 256)) & 0xFFFF
        return bytes([raw >> 8, raw & 0xFF])


class _FakeInterface:
    def __init__(self, left=None, right=None):
        self.left = left
        self.right = right


def test_q88_positive_and_negative():
    assert q88_to_celsius(bytes([0x3C, 0x80])) == pytest.approx(60.5)
    assert q88_to_celsius(bytes([0xFF, 0x00])) == pytest.approx(-1.0)


def test_read_camera_temps_mixed_outcomes():
    left = _FakeSensor({0: 45.0, 1: 113.5, 2: False})     # cams 3-7 raise
    iface = _FakeInterface(left=left, right=None)
    temps = read_camera_temps(iface)
    assert temps[("left", 0)] == pytest.approx(45.0)
    assert temps[("left", 1)] == pytest.approx(113.5)
    assert math.isnan(temps[("left", 2)])                  # fw error -> NaN
    assert math.isnan(temps[("left", 7)])                  # raise -> NaN
    assert not any(side == "right" for side, _ in temps)   # absent side skipped


def test_read_camera_temps_disconnected_sensor_skipped():
    left = _FakeSensor({0: 45.0}, connected=False)
    temps = read_camera_temps(_FakeInterface(left=left))
    assert temps == {} and left.read_calls == 0


def test_read_camera_temps_none_interface():
    assert read_camera_temps(None) == {}


# ---------------------------------------------------------------------------
# CooldownPolicy — temperature gating
# ---------------------------------------------------------------------------

from thermal_cooldown import CooldownPolicy  # noqa: E402

_CFG = {
    "cooldownEnabled": True,
    "cooldownStartTempC": 45.0,
    "cooldownHysteresisC": 2.0,
    "cooldownTimerFallbackSec": 600,
    "cooldownTauSec": 0,
    "cooldownAmbientC": 25.0,
}


def _temps(*vals):
    return {("left", i): float(v) for i, v in enumerate(vals)}


def _armed(cfg=None):
    """Policy armed by a scan end at t=0 — the state in which temps are
    trustworthy and the gate is live (#330 E0: pre-scan reads are garbage)."""
    p = CooldownPolicy(cfg or _CFG)
    p.on_scan_ended(now=0.0)
    return p


def test_policy_locks_above_threshold_and_releases_below_hysteresis():
    p = _armed()
    assert p.apply(_temps(30.0, 80.0), now=0.0) is True
    assert p.locked and p.reason == "temp"
    assert p.hottest_c == pytest.approx(80.0)
    # Between release level (43) and threshold (45): still locked.
    p.apply(_temps(30.0, 44.0), now=5.0)
    assert p.locked
    # At/below threshold - hysteresis: released AND disarmed.
    changed = p.apply(_temps(30.0, 43.0), now=10.0)
    assert changed and not p.locked and p.reason == ""
    assert not p.armed


def test_policy_unarmed_ignores_snapshots():
    p = CooldownPolicy(_CFG)
    # Boot-state garbage (E0): a hot-looking junk read must not lock.
    p.apply(_temps(57.2, 120.0), now=0.0)
    assert not p.locked and math.isnan(p.hottest_c)


def test_policy_no_lock_when_never_above_threshold():
    p = _armed()
    p.apply(_temps(44.9), now=0.0)
    assert not p.locked


def test_policy_judges_on_finite_temps_only():
    p = _armed()
    p.apply({("left", 0): float("nan"), ("left", 1): 90.0}, now=0.0)
    assert p.locked and p.hottest_c == pytest.approx(90.0)


def test_policy_disabled_never_locks():
    p = _armed({**_CFG, "cooldownEnabled": False})
    p.apply(_temps(120.0), now=0.0)
    assert not p.locked


def test_policy_change_detection_is_stable():
    p = _armed()
    assert p.apply(_temps(80.0), now=0.0) is True
    assert p.apply(_temps(80.0), now=5.0) is False       # nothing changed
    assert p.apply(_temps(79.0), now=10.0) is True       # hottest moved


# ---------------------------------------------------------------------------
# CooldownPolicy — timer fallback, override, ETA
# ---------------------------------------------------------------------------

def test_policy_timer_fallback_when_no_temps():
    p = CooldownPolicy(_CFG)
    p.on_scan_ended(now=100.0)
    p.apply({}, now=101.0)                       # nothing readable
    assert p.locked and p.reason == "timer"
    assert p.eta_sec(now=101.0) == 599
    p.apply({}, now=100.0 + 600.0)               # expiry -> fail open + disarm
    assert not p.locked and not p.armed


def test_policy_no_timer_no_temps_fails_open():
    p = CooldownPolicy(_CFG)
    p.apply({}, now=0.0)                          # fresh launch, all NaN
    assert not p.locked


def test_policy_temps_take_authority_over_timer():
    p = CooldownPolicy(_CFG)
    p.on_scan_ended(now=0.0)
    p.apply(_temps(30.0), now=1.0)                # readable AND cool
    assert not p.locked                           # timer ignored


def test_policy_override_unlocks_until_next_scan_end():
    p = _armed()
    p.apply(_temps(90.0), now=0.0)
    assert p.locked
    p.override()
    p.apply(_temps(90.0), now=1.0)
    assert not p.locked
    p.on_scan_ended(now=2.0)                      # override consumed
    p.apply(_temps(90.0), now=3.0)
    assert p.locked


def test_policy_eta_from_tau():
    p = _armed({**_CFG, "cooldownTauSec": 300})
    p.apply(_temps(90.0), now=0.0)
    # tau * ln((90-25)/(43-25)) = 300 * ln(65/18) ~= 385 s
    assert p.eta_sec(now=0.0) == pytest.approx(385, abs=2)


def test_policy_eta_unknown_without_tau():
    p = _armed({**_CFG, "cooldownTauSec": 0})
    p.apply(_temps(90.0), now=0.0)
    assert p.eta_sec(now=0.0) == -1


# ---------------------------------------------------------------------------
# CooldownGate — Qt shell (constructed without a QApplication; ticks are
# driven synchronously so no thread or event loop is involved)
# ---------------------------------------------------------------------------

from thermal_cooldown import CooldownGate  # noqa: E402


def _make_gate(iface, idle=True, cfg=None):
    state = {"idle": idle, "clock": 0.0}
    gate = CooldownGate(
        cfg or _CFG,
        interface_getter=lambda: iface,
        is_idle_fn=lambda: state["idle"],
        clock=lambda: state["clock"],
    )
    return gate, state


def test_gate_tick_applies_snapshot_and_signals():
    left = _FakeSensor({i: 30.0 for i in range(8)})
    left._temps[3] = 90.0
    gate, _ = _make_gate(_FakeInterface(left=left))
    gate.on_scan_ended()       # arm — temps only trusted post-scan
    fired = []
    gate.stateChanged.connect(lambda: fired.append(1))
    gate._tick_once()          # same-thread emit -> synchronous delivery
    assert gate.policy.locked and fired == [1]


def test_gate_tick_skips_bus_when_unarmed():
    left = _FakeSensor({i: 90.0 for i in range(8)})
    gate, _ = _make_gate(_FakeInterface(left=left))
    gate._tick_once()          # never armed -> no I2C traffic at all
    assert left.read_calls == 0 and not gate.policy.locked


def test_gate_tick_skips_bus_when_not_idle():
    left = _FakeSensor({i: 30.0 for i in range(8)})
    gate, state = _make_gate(_FakeInterface(left=left), idle=False)
    gate.on_scan_ended()
    gate._tick_once()
    assert left.read_calls == 0


def test_gate_override_reflects_immediately():
    left = _FakeSensor({i: 90.0 for i in range(8)})
    gate, _ = _make_gate(_FakeInterface(left=left))
    gate.on_scan_ended()
    gate._tick_once()
    assert gate.policy.locked
    gate.request_override()
    assert not gate.policy.locked


def test_gate_disabled_never_starts_thread():
    gate, _ = _make_gate(_FakeInterface(), cfg={**_CFG, "cooldownEnabled": False})
    gate.start()
    assert gate._thread is None


def test_connector_exposes_cooldown_surface():
    import motion_connector
    cls = motion_connector.MotionConnector
    for name in ("cooldownLockout", "cooldownHottestTempC",
                 "cooldownThresholdC", "cooldownEtaSec", "cooldownReason",
                 "cooldownOverride", "cooldownStateChanged"):
        assert hasattr(cls, name), name
