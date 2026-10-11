"""The sensors of each MPPT module: current, voltage, power and lifetime energy."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from .coordinator import FroniusRuntimeData
from .descriptions import FroniusSensorDescription
from .fronius_modbus_api.device import REPORT_MPPT


def _mppt_module_value(
    index: int, attribute: str
) -> Callable[[FroniusRuntimeData], Any]:
    """A value_fn reading one field off an MPPT module, bound to that module."""
    return lambda runtime: getattr(runtime.device.mppt_module(index), attribute)


_MPPT_MODULE_SENSOR_SPECS = (
    (
        "dc_current",
        "dca",
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
        "A",
    ),
    (
        "dc_voltage",
        "dcv",
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
        "V",
    ),
    (
        "dc_power",
        "dcw",
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
        "W",
    ),
    (
        "lifetime_energy",
        "dcwh",
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
        "Wh",
    ),
)


def _left_out(
    runtime: FroniusRuntimeData, index: int, key: str, attribute: str
) -> bool:
    """Whether a sensor waits for its first value.

    A string in the dark sends no value; a restart then deleted the sensor the
    owner had (Codex C-03), so a registered one stays.
    """
    value = getattr(runtime.device.mppt_module(index), attribute, None)
    return value is None and key not in runtime.registered_keys


def left_out_mppt_keys(runtime: FroniusRuntimeData) -> frozenset[str]:
    """The keys of the MPPT sensors left out until their string sends a value."""
    return frozenset(
        key
        for index in runtime.device.mppt_channels.pv
        for suffix, attribute, *_ in _MPPT_MODULE_SENSOR_SPECS
        if _left_out(runtime, index, key := f"mppt_module_{index}_{suffix}", attribute)
    )


def _mppt_sensor_descriptions(
    index: int, runtime: FroniusRuntimeData
) -> list[FroniusSensorDescription]:
    descriptions = []
    for (
        suffix,
        attribute,
        device_class,
        state_class,
        unit,
    ) in _MPPT_MODULE_SENSOR_SPECS:
        key = f"mppt_module_{index}_{suffix}"
        if _left_out(runtime, index, key, attribute):
            continue
        descriptions.append(
            FroniusSensorDescription(
                key=key,
                translation_key=f"mppt_module_{suffix}",
                translation_placeholders={"module": str(index)},
                device="inverter",
                report_name=REPORT_MPPT,
                value_fn=_mppt_module_value(index, attribute),
                device_class=device_class,
                state_class=state_class,
                native_unit_of_measurement=unit,
            )
        )
    return descriptions
