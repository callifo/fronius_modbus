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
        if getattr(runtime.device.mppt_module(index), attribute, None) is None:
            continue
        descriptions.append(
            FroniusSensorDescription(
                key=f"mppt_module_{index}_{suffix}",
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
