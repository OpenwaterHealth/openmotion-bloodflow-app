"""Hardware access layer for the camera-drift campaign.

Wraps the omotion SDK with campaign-specific helpers:
- connect/bring-up of console + both sensors
- OX02C1B register read (via OW_CMD_I2C_REG_READ, 16-bit addr, mux channel)
- OX02C1B register write (switch_camera + OW_I2C_PASSTHRU)
- range dumps / diffs / verified apply
- per-camera die temperature

SAFETY: never touches laser registers or console laser config beyond the
SDK-standard `apply_laser_power()` baseline load (identical to the app's
connect path). Camera writes are restricted to device 0x36.

Register-write caveat: the FW read path (OW_CMD_I2C_REG_READ) disables all
TCA9548A channels after each read, while the write path (OW_I2C_PASSTHRU)
uses the channel selected by switch_camera. write_reg therefore ALWAYS
switches first. Avoid interleaving reads/writes during active streaming —
firmware reads camera temps over the same bus per frame.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from omotion.MotionInterface import MotionInterface
from omotion.i2c_packet import I2C_Packet

logger = logging.getLogger("campaign.owcam")

CAM_ADDR = 0x36  # OX02C1B 7-bit I2C address behind the TCA9548A


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(timeout_s: float = 30.0, data_dir: str | None = None) -> MotionInterface:
    """Bring up the interface and block until console + both sensors connect."""
    iface = MotionInterface(data_dir=data_dir)
    iface.start(wait=True, wait_timeout=5.0)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        ok_console = iface.console.is_connected()
        ok_left = iface.left.is_connected()
        ok_right = iface.right.is_connected()
        if ok_console and ok_left and ok_right:
            logger.info("console + left + right connected")
            return iface
        time.sleep(0.5)
    raise TimeoutError(
        f"connect timeout: console={iface.console.is_connected()} "
        f"left={iface.left.is_connected()} right={iface.right.is_connected()}"
    )


def sensor_for(iface: MotionInterface, side: str):
    s = getattr(iface, side)
    if not s.is_connected():
        raise RuntimeError(f"{side} sensor not connected")
    return s


# ---------------------------------------------------------------------------
# Register access
# ---------------------------------------------------------------------------

def read_reg(sensor, cam: int, addr: int, n: int = 1) -> bytes:
    """Read n bytes from camera register(s) starting at addr (16-bit)."""
    data = sensor.i2c_read_register(
        CAM_ADDR, addr, read_len=n, reg_addr_size=2, mux_channel=cam
    )
    if data is False or data is None:
        raise IOError(f"read_reg failed cam{cam} @0x{addr:04X} x{n}")
    return data


def write_reg(sensor, cam: int, addr: int, val: int) -> None:
    """Write one byte to a camera register. Always re-selects the mux channel
    (reads clear the mux selection)."""
    r = sensor.switch_camera(cam)
    if r.packetType in _error_types():
        raise IOError(f"switch_camera({cam}) failed")
    ok = sensor.camera_i2c_write(
        I2C_Packet(device_address=CAM_ADDR, register_address=addr, data=val & 0xFF)
    )
    if not ok:
        raise IOError(f"write_reg failed cam{cam} @0x{addr:04X}=0x{val:02X}")


def _error_types():
    from omotion.MotionSensor import _ERROR_TYPES
    return _ERROR_TYPES


def apply_writes(sensor, cam: int, writes: list[tuple[int, int]],
                 verify: bool = True, settle_s: float = 0.02) -> list[str]:
    """Apply a list of (addr, val) writes to one camera; verify by readback.

    Returns a list of mismatch strings (empty = all verified). Write-only /
    self-clearing registers can't verify — caller filters those if needed.
    """
    mismatches: list[str] = []
    for addr, val in writes:
        write_reg(sensor, cam, addr, val)
        time.sleep(settle_s)
    if verify:
        for addr, val in writes:
            got = read_reg(sensor, cam, addr, 1)[0]
            if got != (val & 0xFF):
                mismatches.append(
                    f"cam{cam} @0x{addr:04X}: wrote 0x{val:02X} read 0x{got:02X}"
                )
    return mismatches


# Ranges that cover everything drift-relevant. Read in single bulk calls
# (FW supports up to 256 B per read).
DUMP_RANGES: list[tuple[int, int, str]] = [
    (0x0100, 0x10, "mode"),        # mode select / sw standby / fast mode
    (0x3000, 0x20, "system"),      # chip id, lane config
    (0x3500, 0x50, "aec"),         # exposure/gain/manual ctrl
    (0x3800, 0x90, "timing"),      # window, HTS/VTS, trigger mode
    (0x3B00, 0x30, "strobe"),      # strobe config
    (0x4000, 0xA0, "blc"),         # BLC control/targets/offsets + applied-offset readbacks (0x4072+)
    (0x4D00, 0x40, "tpm"),         # temperature sensor + cal
    (0x5000, 0x10, "isp"),         # pre-ISP enables
    (0x5100, 0x10, "testpat"),     # test pattern
    (0x4800, 0x10, "mipi"),        # mipi ctrl (linesync bit)
    (0x4C00, 0x28, "avg"),         # on-chip Yavg statistics
    (0x4F00, 0x10, "watchdog"),    # fault summary (blc_fault, tpm_fault, otp_fault)
]


def read_blc_offsets(sensor, cam: int) -> dict[int, int]:
    """Applied BLC offsets (read-only, 15-bit) for channels 0-7."""
    base_pairs = [0x4072, 0x4076, 0x407A, 0x407E, 0x4082, 0x4086, 0x408A, 0x408E]
    data = read_reg(sensor, cam, 0x4072, 0x30)
    out = {}
    for ch, addr in enumerate(base_pairs):
        off = addr - 0x4072
        out[ch] = ((data[off] & 0x7F) << 8) | data[off + 1]
    return out


def dump_registers(sensor, cam: int) -> dict[int, int]:
    """Dump all campaign-relevant register ranges for one camera."""
    out: dict[int, int] = {}
    for base, count, _name in DUMP_RANGES:
        data = read_reg(sensor, cam, base, count)
        for i, b in enumerate(data):
            out[base + i] = b
    return out


def diff_dumps(a: dict[int, int], b: dict[int, int]) -> list[str]:
    """Human-readable diff of two register dumps (a -> b)."""
    lines = []
    for addr in sorted(set(a) | set(b)):
        va, vb = a.get(addr), b.get(addr)
        if va != vb:
            fa = f"0x{va:02X}" if va is not None else "--"
            fb = f"0x{vb:02X}" if vb is not None else "--"
            lines.append(f"0x{addr:04X}: {fa} -> {fb}")
    return lines


def dump_to_text(dump: dict[int, int]) -> str:
    lines = []
    for base, count, name in DUMP_RANGES:
        lines.append(f"# {name}")
        for addr in range(base, base + count):
            if addr in dump:
                lines.append(f"0x{addr:04X} 0x{dump[addr]:02X}")
    return "\n".join(lines) + "\n"


def read_chip_id(sensor, cam: int) -> int:
    """Chip ID from 0x300A/0x300B (+0x300C if used)."""
    data = read_reg(sensor, cam, 0x300A, 2)
    return (data[0] << 8) | data[1]


def read_die_temp_c(sensor, cam: int) -> float:
    """Read the TPM die temperature directly (0x4D2A/0x4D2B, Q8.8 signed)."""
    data = read_reg(sensor, cam, 0x4D2A, 2)
    raw = (data[0] << 8) | data[1]
    if raw >= 0x8000:
        raw -= 0x10000
    return raw / 256.0


# ---------------------------------------------------------------------------
# Session bring-up helpers
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    chip_ids: dict[str, dict[int, int]]
    temps: dict[str, dict[int, float]]


def survey(iface: MotionInterface, cams: list[int]) -> SessionInfo:
    """Read chip IDs + die temps for the given cameras on both sides."""
    info = SessionInfo(chip_ids={}, temps={})
    for side in ("left", "right"):
        sensor = sensor_for(iface, side)
        info.chip_ids[side] = {}
        info.temps[side] = {}
        for cam in cams:
            try:
                info.chip_ids[side][cam] = read_chip_id(sensor, cam)
                info.temps[side][cam] = read_die_temp_c(sensor, cam)
            except IOError as e:
                logger.warning("survey %s cam%d: %s", side, cam, e)
                info.chip_ids[side][cam] = -1
                info.temps[side][cam] = float("nan")
    return info
