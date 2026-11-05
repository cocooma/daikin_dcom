"""Constants for the Daikin Altherma 3 DCOM-LT/MB integration.

Register numbers are the "Register Offset" values from the DCOM-LT/MB
reference manual (register base 0, i.e. offset == Modbus PDU address).
"""
from __future__ import annotations

DOMAIN = "daikin_dcom"

CONF_SLAVE = "slave"
CONF_FRAMING = "framing"
CONF_CONTROL_MODE = "control_mode"
CONF_SCAN_INTERVAL = "scan_interval"

FRAMING_TCP = "tcp"  # native Modbus TCP (MBAP header)
FRAMING_RTU = "rtu"  # RTU frames tunnelled over TCP (typical RS485 gateway)

CONTROL_LWT = "lwt"  # leaving water temperature setpoints (regs 1 / 2)
CONTROL_ROOM = "room"  # room thermostat setpoints (regs 6 / 7)

DEFAULT_PORT = 502
DEFAULT_SLAVE = 1
DEFAULT_FRAMING = FRAMING_TCP
DEFAULT_CONTROL_MODE = CONTROL_LWT
DEFAULT_SCAN_INTERVAL = 30  # seconds

# Special return values (see "SPECIAL RETURN VALUES" in the manual)
REG_UNSUPPORTED = 32767
REG_UNAVAILABLE = 32766
REG_WAIT_FOR_VALUE = 32765
SENTINELS = {REG_UNSUPPORTED, REG_UNAVAILABLE, REG_WAIT_FOR_VALUE}

# --- Holding registers (function 3 / 6 / 16) --------------------------------
H_LWT_HEAT_SP = 1  # 25..55 degC
H_LWT_COOL_SP = 2  # 5..22 degC
H_OPERATION_MODE = 3  # 0 auto, 1 heating, 2 cooling
H_SPACE_ONOFF = 4  # 0 off, 1 on
H_ROOM_HEAT_SP = 6  # 12..30 degC
H_ROOM_COOL_SP = 7  # 15..35 degC
H_QUIET_MODE = 9  # 0 off, 1 on
H_DHW_SP = 10  # 30..60 degC
H_DHW_ONOFF = 12  # 0 off, 1 on (DHW reheat)
H_DHW_BOOSTER = 13  # 0 off, 1 on
H_WEATHER_MODE = 53  # 0 fixed, 1 weather dep., 2 fixed+sched., 3 weather dep.+sched.
H_WEATHER_HEAT_OFFSET = 54  # -10..10 degC
H_WEATHER_COOL_OFFSET = 55  # -10..10 degC

# --- Input registers (function 4) --------------------------------------------
I_UNIT_ERROR = 21  # 0 none, 1 fault, 2 warning
I_ERROR_CODE = 22  # text16
I_ERROR_SUBCODE = 23
I_PUMP = 30
I_COMPRESSOR = 31
I_BOOSTER_HEATER = 32
I_DISINFECTION = 33
I_DEFROST = 35
I_HOT_START = 36
I_THREE_WAY_VALVE = 37  # 0 space heating, 1 DHW
I_OPERATION_MODE = 38  # 1 heating, 2 cooling
I_LWT_PRE_PHE = 40  # temp16
I_LWT_PRE_BUH = 41  # temp16
I_RETURN_WATER = 42  # temp16
I_DHW_TEMP = 43  # temp16
I_OUTSIDE_TEMP = 44  # temp16
I_REFRIGERANT_TEMP = 45  # temp16
I_FLOW_RATE = 49  # l/min x 100
I_ROOM_TEMP = 50  # temp16 (Remocon)

# Contiguous documented blocks: (kind, start, count). Undocumented gaps are
# not requested so that no illegal-address exception is provoked.
READ_BLOCKS: tuple[tuple[str, int, int], ...] = (
    ("h", 1, 4),
    ("h", 6, 2),
    ("h", 9, 2),
    ("h", 12, 2),
    ("h", 53, 3),
    ("i", 21, 3),
    ("i", 30, 4),
    ("i", 35, 4),
    ("i", 40, 6),
    ("i", 49, 2),
)

# Setpoint ranges (degC)
RANGES = {
    CONTROL_LWT: {"heat": (25, 55), "cool": (5, 22)},
    CONTROL_ROOM: {"heat": (12, 30), "cool": (15, 35)},
}
DHW_RANGE = (30, 60)

WEATHER_MODES = {
    0: "fixed",
    1: "weather_dependent",
    2: "fixed_scheduled",
    3: "weather_dependent_scheduled",
}
