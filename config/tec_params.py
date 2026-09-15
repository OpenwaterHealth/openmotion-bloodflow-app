"""TEC DAC setpoints by console hardware revision (issue #269).

EVT2 units need +1.16 V on the TEC DAC to hold the lasers at 25 C; DVT1a
changed the voltage divider around the DAC input, so DVT-and-beyond use
``TEC_VOLTAGE_DEFAULT``. The unit revision comes from the console's 3-bit
BRD_V0..V2 hardware strap (SDK ``console.read_board_id()``): EVT2 straps 1,
the DVT1a Unified Console Board straps 2. The SDK returns 0 on a transport
error, so 0 must never be listed as an EVT2 id.

Formerly ``config/tec_params.json``; compiled into the application since
#546 so nothing editable ships next to the executable.
"""

TEC_VOLTAGE_DEFAULT = -0.07   # volts, DVT1a and beyond
TEC_VOLTAGE_EVT2 = 1.16       # volts, EVT2 units
EVT2_BOARD_IDS = (1,)         # board-ID strap values that mean "EVT2"
