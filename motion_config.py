"""TEC-parameter helper.

Loads `config/tec_params.json` (the TEC voltage default). Extracted from
`motion_connector.py` to keep that file focused on the Qt connector.

The laser-power application and FPGA register map that used to live here now
live in the SDK (`omotion.laser` — bundled `laser_params.json` / `fpga_model.json`
and `apply_laser_power`), so the SDK is the single owner of that config. The
thermistor R-T lookup likewise lives in `omotion.console_telemetry_conversions`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from utils.resource_path import resource_path


logger = logging.getLogger("ow-testapp")


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
