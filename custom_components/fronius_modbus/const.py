"""Constants, SunSpec state maps, and shared value-mapping helpers."""

from collections.abc import Mapping
import re

DOMAIN = "fronius_modbus"
DEFAULT_NAME = "Fronius"
ENTITY_PREFIX = "fm"
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_WEB_SCAN_INTERVAL = 60
MINIMUM_SCAN_INTERVAL = 5
DEFAULT_PORT = 502
DEFAULT_INVERTER_UNIT_ID = 1
DEFAULT_METER_UNIT_ID = 200
DEFAULT_AUTO_ENABLE_MODBUS = True
DEFAULT_RESTRICT_MODBUS_TO_THIS_IP = False
API_USERNAME = "customer"
TECHNICIAN_USERNAME = "technician"
# The local web logins an entry can use; exactly one is active per entry.
API_USERNAMES = (API_USERNAME, TECHNICIAN_USERNAME)
CONF_RECONFIGURE_REQUIRED = "_reconfigure_required"
MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX = "legacy_modbus_only_reconfigure_"
SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX = "solar_api_low_firmware_"
CONF_INVERTER_UNIT_ID = "inverter_modbus_unit_id"
CONF_METER_UNIT_ID = "meter_modbus_unit_id"
CONF_METER_UNIT_IDS = "meter_modbus_unit_ids"
CONF_API_USERNAME = "api_username"
CONF_API_PASSWORD = "api_password"
CONF_AUTO_ENABLE_MODBUS = "auto_enable_modbus"
CONF_RESTRICT_MODBUS_TO_THIS_IP = "restrict_modbus_to_this_ip"
CONF_WEB_SCAN_INTERVAL = "web_scan_interval"
SUPPORTED_MANUFACTURERS = ["Fronius"]
SUPPORTED_MODELS = ["Primo GEN24", "Symo GEN24", "Verto"]
# Every enum state is a translation key (hassfest: [a-z0-9-_]+); an unmapped
# SunSpec code shows as this key instead of a value the option list lacks.
UNKNOWN_STATE = "unknown"
API_BATTERY_MODE = {
    0: "auto",
    1: "manual",
}

API_SOC_MODE = {
    "auto": "automatic",
    "manual": "manual",
}

STORAGE_CONTROL_MODE = {
    0: "auto",
    1: "charge",
    2: "discharge",
    3: "charge_and_discharge",
}

CHARGE_STATUS = {
    1: "off",
    2: "empty",
    3: "discharging",
    4: "charging",
    5: "full",
    6: "holding",
    7: "testing",
}

INVERTER_STATUS = {
    1: "off",
    2: "sleeping",
    3: "starting",
    4: "normal",
    5: "throttled",
    6: "shutdown",
    7: "fault",
    8: "standby",
}

INVERTER_CONTROLS = [
    "power_reduction",
    "constant_reactive_power",
    "constant_power_factor",
]

INVERTER_EVENTS = [
    "error",
    "warning",
    "info",
]

FRONIUS_INVERTER_STATUS = {
    1: "off",
    2: "sleeping",
    3: "starting",
    4: "normal",
    5: "throttled",
    6: "shutdown",
    7: "fault",
    8: "standby",
    9: "no_solarnet",
    10: "no_inverter_communication",
    11: "overcurrent_solarnet",
    12: "firmware_updating",
    13: "acfi_event",
}

CHARGE_GRID_STATUS = {
    0: "disabled",
    1: "enabled",
}

# Why the inverter is limiting its output, in the order the rule reports them.
THROTTLE_REASONS = (
    "none",
    "inverter_state",
    "active_power_control",
    "export_limit",
    "several",
)

GRID_STATUS = {
    0: "off_grid",
    1: "off_grid_operating",
    2: "on_grid",
    3: "on_grid_operating",
}

CONNECTION_STATUS_CONDENSED = {
    0: "disconnected",
    1: "connected",
    3: "available",
    7: "operating",
}

ECP_CONNECTION_STATUS = {
    0: "disconnected",
    1: "connected",
}

CONTROL_STATUS = {
    0: "disabled",
    1: "enabled",
}

AC_LIMIT_STATUS = {
    0: "disabled",
    1: "enabled",
}

STORAGE_EXT_CONTROL_MODE = {
    0: "auto",
    1: "pv_charge_limit",
    2: "discharge_limit",
    3: "pv_charge_and_discharge_limit",
    4: "charge_from_grid",
    5: "discharge_to_grid",
    6: "block_discharging",
    7: "block_charging",
}


def _state_values(*mappings: Mapping[int, str]) -> list[str]:
    return list(
        dict.fromkeys(
            [value for mapping in mappings for value in mapping.values()]
            + [UNKNOWN_STATE]
        )
    )


INVERTER_CONTROL_STATE_VALUES = [
    "normal",
    "power_reduction",
    "constant_reactive_power",
    "constant_power_factor",
    "power_reduction_constant_reactive_power",
    "power_reduction_constant_power_factor",
    "constant_reactive_power_constant_power_factor",
    "power_reduction_constant_reactive_power_constant_power_factor",
]


SENSOR_STATE_OPTIONS = {
    "pv_connection": _state_values(CONNECTION_STATUS_CONDENSED),
    "storage_connection": _state_values(CONNECTION_STATUS_CONDENSED),
    "ecp_connection": _state_values(ECP_CONNECTION_STATUS),
    "status": _state_values(INVERTER_STATUS),
    "statusvendor": _state_values(FRONIUS_INVERTER_STATUS),
    "grid_status": _state_values(GRID_STATUS),
    "throttle_reason": [*THROTTLE_REASONS, UNKNOWN_STATE],
    "connection_control": _state_values(CONTROL_STATUS),
    "power_limit_control": _state_values(CONTROL_STATUS),
    "power_factor_control": _state_values(CONTROL_STATUS),
    "reactive_power_control": _state_values(CONTROL_STATUS),
    "ac_limit_enable": _state_values(AC_LIMIT_STATUS, {2: UNKNOWN_STATE}),
    "control_mode": _state_values(STORAGE_CONTROL_MODE) + INVERTER_CONTROL_STATE_VALUES,
    "charge_status": _state_values(CHARGE_STATUS),
    "grid_charging": _state_values(CHARGE_GRID_STATUS),
    "api_modbus_control": _state_values(CONTROL_STATUS),
    "api_modbus_restriction": _state_values(CONTROL_STATUS),
}


_NOT_A_KEY_CHARACTER = re.compile(r"[^a-z0-9]+")


def instance_key(entry_id: str) -> str:
    """The per-entry key every unique id and device identifier is built on since 0.2."""
    return _NOT_A_KEY_CHARACTER.sub("_", entry_id.lower()).strip("_") or "fronius"


def entity_prefix(entry_id: str) -> str:
    """The unique-id/entity-id prefix for one config entry."""
    return f"{ENTITY_PREFIX}_{instance_key(entry_id)}"


def map_code(mapping: dict[int, str], code: int | None) -> str | None:
    """Look up a SunSpec state code; an unmapped one reads as UNKNOWN_STATE."""
    if code is None:
        return None
    return mapping.get(code, UNKNOWN_STATE)


def bitmask_to_string(
    bitmask: int | None, names: list[str], default: str, bits: int = 16
) -> str | None:
    """Render a SunSpec bitfield as its set flag names joined into one state key."""
    if bitmask is None:
        return None
    set_names = [
        names[bit] if bit < len(names) else f"bit {bit} undefined"
        for bit in range(bits)
        if bitmask & (1 << bit)
    ]
    return "_".join(set_names) if set_names else default
