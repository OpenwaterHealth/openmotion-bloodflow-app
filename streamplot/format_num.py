"""Small number-formatting helpers used by the streamplot tools.

This module provides a minimal `format_three_nonzero_decimals` implementation
to avoid requiring an external dependency. The function formats a numeric
value with up to three decimal places and strips trailing zeros.
"""
from typing import Any

def format_three_nonzero_decimals(value: Any) -> str:
    """Format `value` as a string with up to three decimal places.

    - If `value` is None, returns an empty string.
    - Rounds to 3 decimal places and strips trailing zeros and the decimal
      point when not needed (e.g. 1.200 -> "1.2", 1.000 -> "1").
    """
    if value is None:
        return ""
    try:
        f = float(value)
    except Exception:
        return str(value)
    # Use rounding to 3 decimal places, then strip trailing zeros
    s = f"{f:.3f}".rstrip('0').rstrip('.')
    # For very small values that round to an empty string like "0.000" -> "",
    # ensure we return "0".
    return s if s != "" else "0"

__all__ = ["format_three_nonzero_decimals"]
