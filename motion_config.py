"""FPGA model and laser-parameter helpers.

This module encapsulates loading and querying `models/fpga_model.json`
(and the legacy `models/FpgaModel.js` fallback) as well as loading and
applying `config/laser_params.json`. It was extracted from
`motion_connector.py` to keep that file focused on the Qt connector.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from utils.resource_path import resource_path


logger = logging.getLogger("ow-testapp")


_MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
_FPGA_JSON_PATH = os.path.join(_MODELS_DIR, "fpga_model.json")
_FPGA_JS_PATH = os.path.join(_MODELS_DIR, "FpgaModel.js")


class FpgaModel:
    """Encapsulates the FPGA model data and scale-override/caching logic."""

    def __init__(self) -> None:
        self._model: Optional[list] = None
        self._scale_overrides: dict[str, float] = {}
        self._scale_cache: dict[tuple, float] = {}

        try:
            if os.path.exists(_FPGA_JSON_PATH):
                with open(_FPGA_JSON_PATH, "r", encoding="utf-8") as _f:
                    self._model = json.load(_f)
                logger.info(f"Loaded FPGA model JSON from {_FPGA_JSON_PATH}")
            else:
                logger.warning(f"FPGA model JSON not found at {_FPGA_JSON_PATH}")
        except Exception as e:
            logger.error(f"Failed to load FPGA model JSON: {e}")
            self._model = None

    @property
    def model(self) -> list:
        """Return the raw loaded FPGA model (list of dicts) or empty list."""
        return self._model if self._model is not None else []

    def set_scale_override(self, label: str, name: str, scale: float) -> None:
        """Set or clear a runtime scale override used by `get_scale`.

        Pass `scale <= 0` to remove the override.
        """
        key = f"{label}|{name}"
        try:
            if scale > 0:
                self._scale_overrides[key] = float(scale)
            else:
                self._scale_overrides.pop(key, None)
            # Invalidate cached entry for this key so the override takes effect.
            self._scale_cache.pop((label, name), None)
        except Exception:
            pass

    def get_scale(self, label: str, name: str) -> Optional[float]:
        """Retrieve the scale factor for a given `label` and function `name`.

        Returns float scale on success or None on failure. Caches results.
        """
        key = (label, name)
        if key in self._scale_cache:
            return self._scale_cache[key]

        try:
            # Check overrides first
            ov_key = f"{label}|{name}"
            if ov_key in self._scale_overrides:
                scale = float(self._scale_overrides[ov_key])
                self._scale_cache[key] = scale
                return scale

            if self._model:
                for fpga in self._model:
                    if fpga.get("label") == label:
                        for fn in fpga.get("functions", []):
                            if fn.get("name") == name or fn.get("friendlyName") == name:
                                scale = (
                                    float(fn.get("scale"))
                                    if fn.get("scale") is not None
                                    else 1.0
                                )
                                self._scale_cache[key] = scale
                                return scale

            # Fallback: legacy FpgaModel.js parsing
            if os.path.exists(_FPGA_JS_PATH):
                with open(_FPGA_JS_PATH, "r", encoding="utf-8") as f:
                    txt = f.read()

                label_re = re.compile(
                    r'label\s*:\s*"'
                    + re.escape(label)
                    + r'"\s*,.*?functions\s*:\s*\[(.*?)\]',
                    re.S,
                )
                m_label = label_re.search(txt)
                if not m_label:
                    return None

                functions_block = m_label.group(1)
                fn_re = re.compile(
                    r'\{[^}]*name\s*:\s*"'
                    + re.escape(name)
                    + r'"[^}]*scale\s*:\s*([0-9]+(?:\.[0-9]+)?)',
                    re.S,
                )
                m_fn = fn_re.search(functions_block)
                if not m_fn:
                    return None

                scale = float(m_fn.group(1))
                self._scale_cache[key] = scale
                return scale
        except Exception as e:
            logging.debug(f"Failed to read FPGA model scale for {label}/{name}: {e}")
            return None

    def get_entry_by_friendly_name(self, friendlyName: str) -> Optional[dict]:
        """Lookup an FPGA function entry by `friendlyName`.

        Returns a dict with keys: label, mux_idx, channel, i2c_addr, isMsbFirst,
        start_address, data_size, scale (may be None) or None if not found.
        """
        try:
            if self._model:
                for fpga in self._model:
                    for fn in fpga.get("functions", []):
                        if (
                            fn.get("friendlyName") == friendlyName
                            or fn.get("name") == friendlyName
                        ):
                            return {
                                "label": fpga.get("label"),
                                "mux_idx": fpga.get("mux_idx"),
                                "channel": fpga.get("channel"),
                                "i2c_addr": fpga.get("i2c_addr"),
                                "isMsbFirst": fpga.get("isMsbFirst", False),
                                "start_address": fn.get("start_address"),
                                "data_size": fn.get("data_size"),
                                "scale": fn.get("scale"),
                            }

            # Fallback: legacy JS parsing
            if os.path.exists(_FPGA_JS_PATH):
                with open(_FPGA_JS_PATH, "r", encoding="utf-8") as f:
                    txt = f.read()

                block_re = re.compile(
                    r"\{[^}]*label\s*:\s*\"(.*?)\"[^}]*functions\s*:\s*\[(.*?)\][^}]*\}",
                    re.S,
                )
                for m in block_re.finditer(txt):
                    label = m.group(1)
                    outer = m.group(0)
                    functions_block = m.group(2)

                    mux_idx_m = re.search(r"mux_idx\s*:\s*(\d+)", outer)
                    channel_m = re.search(r"channel\s*:\s*(\d+)", outer)
                    i2c_addr_m = re.search(r"i2c_addr\s*:\s*(0x[0-9A-Fa-f]+|\d+)", outer)
                    ismsb_m = re.search(r"isMsbFirst\s*:\s*(true|false)", outer, re.I)

                    for fn in re.finditer(r"\{(.*?)\}", functions_block, re.S):
                        fn_text = fn.group(1)
                        ff_m = re.search(r"friendlyName\s*:\s*\"(.*?)\"", fn_text)
                        if not ff_m:
                            continue
                        if ff_m.group(1) != friendlyName:
                            continue

                        start_addr_m = re.search(
                            r"start_address\s*:\s*(0x[0-9A-Fa-f]+|\d+)", fn_text
                        )
                        data_size_m = re.search(r"data_size\s*:\s*\"(.*?)\"", fn_text)
                        scale_m = re.search(
                            r"scale\s*:\s*([0-9]+(?:\.[0-9]+)?)", fn_text
                        )

                        return {
                            "label": label,
                            "mux_idx": int(mux_idx_m.group(1)) if mux_idx_m else None,
                            "channel": int(channel_m.group(1)) if channel_m else None,
                            "i2c_addr": int(i2c_addr_m.group(1), 0) if i2c_addr_m else None,
                            "isMsbFirst": bool(
                                ismsb_m and ismsb_m.group(1).lower() == "true"
                            ),
                            "start_address": int(start_addr_m.group(1), 0)
                            if start_addr_m
                            else None,
                            "data_size": data_size_m.group(1) if data_size_m else None,
                            "scale": float(scale_m.group(1)) if scale_m else None,
                        }

            return None
        except Exception as e:
            logger.debug(f"get_entry_by_friendly_name error: {e}")
            return None


def load_laser_params(config_dir: str) -> list:
    """Load `laser_params.json` from the given config directory.

    Mirrors the previous `_load_laser_params` behavior on MOTIONConnector.
    Returns a list of parameter dicts, or an empty list on any error.
    """
    config_path = (
        resource_path("config", "laser_params.json")
        if config_dir == "config"
        else Path(config_dir) / "laser_params.json"
    )
    if not config_path.exists():
        logger.error(f"[Connector] Laser parameter file not found: {config_path}")
        return []

    try:
        with open(config_path, "r") as f:
            params = json.load(f)
        logger.info(
            f"[Connector] Loaded {len(params)} laser parameter sets from {config_path}"
        )
        return params
    except FileNotFoundError:
        logger.error(f"[Connector] Laser parameter file not found: {config_path}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"[Connector] Invalid JSON in {config_path}: {e}")
        return []


def load_tec_params(config_dir: str) -> float:
    """Load `tec_params.json` and return the TEC_VOLTAGE_DEFAULT value.

    Returns the float voltage from the file, or the hard-coded default on any error.
    """
    _TEC_VOLTAGE_DEFAULT = -0.07  # volts
    config_path = (
        resource_path("config", "tec_params.json")
        if config_dir == "config"
        else Path(config_dir) / "tec_params.json"
    )

    if not config_path.exists():
        logger.warning(
            f"[Connector] TEC parameter file not found: {config_path}, "
            f"using default value {_TEC_VOLTAGE_DEFAULT}V"
        )
        return _TEC_VOLTAGE_DEFAULT

    try:
        with open(config_path, "r") as f:
            params = json.load(f)
        voltage = params.get("TEC_VOLTAGE_DEFAULT", _TEC_VOLTAGE_DEFAULT)
        logger.info(f"[Connector] Loaded TEC voltage from {config_path}: {voltage}V")
        return voltage
    except FileNotFoundError:
        logger.warning(
            f"[Connector] TEC parameter file not found: {config_path}, "
            f"using default value {_TEC_VOLTAGE_DEFAULT}V"
        )
        return _TEC_VOLTAGE_DEFAULT
    except json.JSONDecodeError as e:
        logger.error(
            f"[Connector] Invalid JSON in {config_path}: {e}, "
            f"using default value {_TEC_VOLTAGE_DEFAULT}V"
        )
        return _TEC_VOLTAGE_DEFAULT
    except Exception as e:
        logger.error(
            f"[Connector] Error loading TEC parameters: {e}, "
            f"using default value {_TEC_VOLTAGE_DEFAULT}V"
        )
        return _TEC_VOLTAGE_DEFAULT


def apply_laser_power_from_config(
    interface: Any,
    laser_params: list,
    fpga_model: FpgaModel,
    console_mutex: Any,
) -> bool:
    """Write laser configuration to the console via I2C.

    Reads user overrides from `interface.console_module.read_config()` and
    applies the `laser_params` list, honoring overrides and DRIVE CL values.
    The `console_mutex` is locked for the duration of the I2C writes.
    """
    logger.info("[Connector] Setting laser power from config...")

    user_cfg: dict = {}
    try:
        cfg_obj = interface.console_module.read_config()
        if cfg_obj is not None:
            user_cfg = cfg_obj.json_data or {}
            print(user_cfg)
    except Exception as _e:
        logger.warning(
            f"[Connector] Could not read user config before laser init: {_e}"
        )

    ee_thresh = user_cfg.get("EE_THRESH")
    ee_gain = user_cfg.get("EE_GAIN")
    opt_thresh = user_cfg.get("OPT_THRESH")
    opt_gain = user_cfg.get("OPT_GAIN")

    # (channel, offset) entries to skip in the JSON pass
    _EE_DRIVE_CL = (6, 0x10)  # Safety EE  DRIVE CL
    _OPT_DRIVE_CL = (7, 0x10)  # Safety OPT DRIVE CL

    skip_entries: set = set()
    if ee_thresh is not None or ee_gain is not None:
        skip_entries.add(_EE_DRIVE_CL)
    if opt_thresh is not None or opt_gain is not None:
        skip_entries.add(_OPT_DRIVE_CL)

    console_mutex.lock()
    try:
        for idx, laser_param in enumerate(laser_params, start=1):
            friendlyName = laser_param["friendlyName"]

            fpga_entry = fpga_model.get_entry_by_friendly_name(friendlyName)
            if fpga_entry is None:
                logger.error(f"Laser parameter entry not found {friendlyName}")
                continue

            muxIdx = fpga_entry["mux_idx"]
            channel = fpga_entry["channel"]
            i2cAddr = fpga_entry["i2c_addr"]
            data_size = fpga_entry["data_size"]
            offset = fpga_entry["start_address"]

            dataToSend = bytearray(laser_param["dataToSend"])

            if (channel, offset) in skip_entries:
                logger.info(
                    f"[Connector] Skipping JSON entry ch={channel} off=0x{offset:02X} "
                    f"(overridden by user config)"
                )
                continue

            if friendlyName in user_cfg:
                override_val = user_cfg[friendlyName]
                logger.info(
                    f"[Connector] Override for {friendlyName}: {override_val}"
                )
                # Parse "8B"/"16B"/"24B"/"32B" → number of bytes
                num_bytes = int(data_size.rstrip("B")) // 8
                scale = fpga_entry.get("scale")
                try:
                    raw_int = float(override_val)
                    if scale:
                        raw_int = raw_int / scale
                    max_val = (1 << (num_bytes * 8)) - 1
                    raw_int = max(0, min(max_val, int(round(raw_int))))
                    is_msb = fpga_entry.get("isMsbFirst", False)
                    byteorder = "big" if is_msb else "little"
                    dataToSend = bytearray(
                        raw_int.to_bytes(num_bytes, byteorder=byteorder)
                    )
                    logger.warning(
                        f"[Connector] Override {friendlyName} raw={raw_int} "
                        f"→ {[f'0x{b:02X}' for b in dataToSend]}"
                    )
                except Exception as _ov_err:
                    logger.warning(
                        f"[Connector] Could not convert override for {friendlyName}: "
                        f"{_ov_err}, using default"
                    )

            logger.info(
                f"[Connector] ({idx}/{len(laser_params)}) "
                f"Writing I2C: muxIdx={muxIdx}, channel={channel}, "
                f"i2cAddr=0x{i2cAddr:02X}, offset=0x{offset:02X}, "
                f"data={[f'0x{b:02X}' for b in dataToSend]}"
            )

            if not interface.console_module.write_i2c_packet(
                mux_index=muxIdx,
                channel=channel,
                device_addr=i2cAddr,
                reg_addr=offset,
                data=dataToSend,
            ):
                logger.error(
                    f"Failed to set laser power (muxIdx={muxIdx}, channel={channel})"
                )
                return False

        # ------------------------------------------------------------------
        # Write user-config DRIVE CL overrides after the JSON pass.
        # Default scale matches the static FpgaModel value (1.86 mA/LSB).
        # Data format: 16-bit LSB-first (isMsbFirst=false in FpgaModel).
        # thresh is a raw uint16 register value; gain is a float scale factor
        # used only for the QML FpgaData scale override.
        # ------------------------------------------------------------------
        def _write_drive_cl(ch: int, thresh, gain, label: str) -> bool:
            if thresh is None:
                return True
            set_value = thresh
            gain_f = float(gain) if gain is not None else 0.0
            if gain_f != 0.0:
                set_value = thresh / gain_f
            raw = max(0, min(0xFFFF, int(round(set_value))))  # uint16 raw value
            data = bytearray([raw & 0xFF, (raw >> 8) & 0xFF])  # LSB first

            logger.info(
                f"[Connector] Writing user-config {label} DRIVE CL: "
                f"raw={raw}, gain={gain_f} → {[f'0x{b:02X}' for b in data]}"
            )
            return interface.console_module.write_i2c_packet(
                mux_index=1, channel=ch, device_addr=0x41, reg_addr=0x10, data=data
            )

        if not _write_drive_cl(6, ee_thresh, ee_gain, "Safety EE"):
            logger.error("Failed to write user-config Safety EE DRIVE CL")
            return False
        if not _write_drive_cl(7, opt_thresh, opt_gain, "Safety OPT"):
            logger.error("Failed to write user-config Safety OPT DRIVE CL")
            return False

        logger.info("Laser power set successfully.")
        return True
    finally:
        console_mutex.unlock()
