"""What the inverter's component endpoints add beyond Modbus: one row per sensor.

`/api/components/inverter/readable` and `.../BatteryManagementSystem/readable`
also carry serial numbers, part serials and device ids. The web client takes
only the fields named here, so none of those reaches a state or a
diagnostics dump.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal

type Component = Literal["inverter", "storage"]
type Transform = Literal["as_is", "positive", "yes_no"]

# Derived fields: the web client builds them from nested attributes.
NAMEPLATE_PREFIX = "nameplate."
CONNECTION = "connection"
# The battery's own firmware and hardware go to its device entry, not to a sensor.
STORAGE_DEVICE_FIELDS = ("sw_version", "hw_version")
YES, NO = "yes", "no"


@dataclass(frozen=True, slots=True)
class ComponentReading:
    """One sensor: the fields it reads and how Home Assistant shows it.

    With several fields the first one the device reports is shown; a yes/no
    reading is "yes" when any of them is set.
    """

    key: str
    component: Component
    fields: tuple[str, ...]
    unit: str | None = None
    device_class: str | None = None
    measurement: bool = True
    diagnostic: bool = False
    enabled: bool = True
    transform: Transform = "as_is"
    suggested_unit: str | None = None


def _module_temperature(index: int) -> ComponentReading:
    return ComponentReading(
        f"module_temperature_{index}",
        "inverter",
        (f"MODULE_TEMPERATURE_MEAN_0{index}_F32",),
        "°C",
        "temperature",
    )


def _feed_in_voltage(suffix: str, channel: str) -> ComponentReading:
    return ComponentReading(
        f"feed_in_voltage_{suffix}",
        "inverter",
        (f"FEEDINPOINT_VOLTAGE_MEAN_{channel}_F32",),
        "V",
        "voltage",
        enabled=False,
    )


def _nameplate(key: str, field: str, unit: str | None) -> ComponentReading:
    return ComponentReading(
        key,
        "storage",
        (NAMEPLATE_PREFIX + field,),
        unit,
        "power" if unit == "W" else None,
        measurement=False,
        diagnostic=True,
        enabled=False,
    )


COMPONENT_READINGS: tuple[ComponentReading, ...] = (
    *(_module_temperature(index) for index in (1, 2, 3, 4)),
    ComponentReading("fan_1", "inverter", ("FANCONTROL_PERCENT_01_F32",), "%"),
    ComponentReading("fan_2", "inverter", ("FANCONTROL_PERCENT_02_F32",), "%"),
    *(
        ComponentReading(
            f"inverter_power_l{phase}",
            "inverter",
            (f"ACBRIDGE_POWERACTIVE_MEAN_0{phase}_F32",),
            "W",
            "power",
        )
        for phase in (1, 2, 3)
    ),
    ComponentReading(
        "production_limit",
        "inverter",
        ("ACBRIDGE_POWERACTIVE_PRODUCTION_LIMIT_F32",),
        "W",
        "power",
    ),
    ComponentReading(
        "production_limit_reached",
        "inverter",
        (
            "ACBRIDGE_VALUE_POWERACTIVE_PRODUCTION_LIMIT_REACHED_U8",
            "ACBRIDGE_VALUE_POWERACTIVE_RELATIVE_PRODUCTION_LIMIT_REACHED_U8",
        ),
        measurement=False,
        transform="yes_no",
    ),
    ComponentReading(
        "battery_max_charge_power",
        "inverter",
        ("DCDC_POWERACTIVE_BAT_MAX_F32",),
        "W",
        "power",
    ),
    ComponentReading(
        "battery_max_discharge_power",
        "inverter",
        ("DCDC_POWERACTIVE_BAT_MIN_F32",),
        "W",
        "power",
        transform="positive",
    ),
    ComponentReading(
        "dc_link_voltage",
        "inverter",
        ("DCLINK_VOLTAGE_MEAN_F32",),
        "V",
        "voltage",
        diagnostic=True,
        enabled=False,
    ),
    ComponentReading(
        "grid_valid",
        "inverter",
        ("FEEDINPOINT_MODE_GRID_VALIDITY_U8",),
        measurement=False,
        transform="yes_no",
    ),
    _feed_in_voltage("l1", "01"),
    _feed_in_voltage("l2", "02"),
    _feed_in_voltage("l3", "03"),
    _feed_in_voltage("l1_l2", "12"),
    _feed_in_voltage("l2_l3", "23"),
    _feed_in_voltage("l3_l1", "31"),
    ComponentReading(
        "feed_in_frequency",
        "inverter",
        ("FEEDINPOINT_FREQUENCY_MEAN_F32",),
        "Hz",
        "frequency",
        enabled=False,
    ),
    # Seconds, but no clock: it grew 7192 s in 7383 s on a GEN24, and a float32
    # at this size steps in 8 s.
    ComponentReading(
        "operating_time",
        "inverter",
        ("DEVICE_TIME_UPTIME_SUM_F32",),
        "s",
        "duration",
        measurement=False,
        diagnostic=True,
        enabled=False,
        suggested_unit="h",
    ),
    ComponentReading(
        "backup_time",
        "inverter",
        ("ACBRIDGE_TIME_BACKUPMODE_UPTIME_SUM_F32",),
        "s",
        "duration",
        measurement=False,
        enabled=False,
        suggested_unit="h",
    ),
    ComponentReading(
        "power_stage_firmware_1",
        "inverter",
        ("PS.rev-sw",),
        measurement=False,
        diagnostic=True,
    ),
    ComponentReading(
        "power_stage_firmware_2",
        "inverter",
        ("PS2.rev-sw",),
        measurement=False,
        diagnostic=True,
    ),
    # Both power stages report the same list of all part numbers, so one sensor.
    ComponentReading(
        "power_stage_hardware",
        "inverter",
        ("PS.rev-hw",),
        measurement=False,
        diagnostic=True,
        enabled=False,
    ),
    ComponentReading(
        "storage_state_of_health",
        "storage",
        ("BAT_VALUE_STATE_OF_HEALTH_RELATIVE_U16",),
        "%",
    ),
    ComponentReading(
        "storage_cell_temperature_min",
        "storage",
        ("BAT_TEMPERATURE_CELL_MIN_F64",),
        "°C",
        "temperature",
        enabled=False,
    ),
    ComponentReading(
        "storage_cell_temperature_max",
        "storage",
        ("BAT_TEMPERATURE_CELL_MAX_F64",),
        "°C",
        "temperature",
        enabled=False,
    ),
    ComponentReading(
        "storage_bms_temperature",
        "storage",
        ("DEVICE_TEMPERATURE_AMBIENTEMEAN_F32",),
        "°C",
        "temperature",
        enabled=False,
    ),
    ComponentReading(
        "storage_discharge_limit",
        "storage",
        ("DCLINK_POWERACTIVE_LIMIT_DISCHARGE_F64",),
        "W",
        "power",
        enabled=False,
    ),
    ComponentReading(
        "storage_power_limit",
        "storage",
        ("DCLINK_POWERACTIVE_MAX_F32",),
        "W",
        "power",
        enabled=False,
    ),
    _nameplate("storage_peak_charge_power", "peak_power_charge_w", "W"),
    _nameplate("storage_peak_discharge_power", "peak_power_discharge_w", "W"),
    _nameplate("storage_rated_soc_min", "min_soc", "%"),
    _nameplate("storage_rated_soc_max", "max_soc", "%"),
    _nameplate("storage_modules", "module_number", None),
    ComponentReading(
        "storage_voltage_min",
        "storage",
        ("min_udc",),
        "V",
        "voltage",
        measurement=False,
        diagnostic=True,
        enabled=False,
    ),
    ComponentReading(
        "storage_voltage_max",
        "storage",
        ("max_udc",),
        "V",
        "voltage",
        measurement=False,
        diagnostic=True,
        enabled=False,
    ),
    ComponentReading(
        "storage_link",
        "storage",
        (CONNECTION,),
        measurement=False,
        diagnostic=True,
        enabled=False,
    ),
)


def readable_fields(component: Component) -> frozenset[str]:
    """Every field the web client may take from a component; nothing else leaves it."""
    fields = {
        field
        for reading in COMPONENT_READINGS
        if reading.component == component
        for field in reading.fields
    }
    if component == "storage":
        fields.update(STORAGE_DEVICE_FIELDS)
    return frozenset(fields)


def component_value(reading: ComponentReading, readings: dict[str, Any] | None) -> Any:
    """What the sensor shows for ``readings``; None while unread or unreported."""
    if readings is None:
        return None
    values = [readings[field] for field in reading.fields if field in readings]
    if not values:
        return None
    if reading.transform == "yes_no":
        return YES if any(values) else NO
    value = values[0]
    if reading.unit is not None and isinstance(value, str):
        value = _as_number(value)
    if value is None or reading.transform != "positive":
        return value
    return abs(value)


def _as_number(text: str) -> float | None:
    """Attributes carry numbers as text ("467.2"); anything else is unknown."""
    try:
        return float(text)
    except ValueError:
        return None


def take_readings(device: dict[str, Any], component: Component) -> dict[str, Any]:
    """The listed fields of one component's readable node; its serials and ids stay behind."""
    channels = device.get("channels")
    channels = channels if isinstance(channels, dict) else {}
    attributes = device.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    nameplate = json_object(attributes.get("nameplate"))
    found = {}
    for field in readable_fields(component):
        value = _reading(field, channels, attributes, nameplate)
        if value is not None:
            found[field] = value
    return found


def _reading(
    field: str,
    channels: dict[str, Any],
    attributes: dict[str, Any],
    nameplate: dict[str, Any],
) -> Any:
    if field.startswith(NAMEPLATE_PREFIX):
        return nameplate.get(field.removeprefix(NAMEPLATE_PREFIX))
    if field == CONNECTION:
        return _connection_text(attributes)
    if field in channels:
        return channels[field]
    return attributes.get(field)


def _connection_text(attributes: dict[str, Any]) -> str | None:
    """How the battery hangs on the inverter, e.g. "ModbusRTU rtu0:21"."""
    connection = json_object(attributes.get("connection"))
    protocol = connection.get("protocol")
    if not protocol:
        return None
    interface = str(connection.get("rtu-interface") or "").rsplit("/", 1)[-1]
    text = f"{protocol} {interface}".strip()
    address = attributes.get("addr")
    return f"{text}:{address}" if address else text


def json_object(value: Any) -> dict[str, Any]:
    """An attribute the inverter nests as a JSON string; anything else is empty."""
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except TypeError, ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
