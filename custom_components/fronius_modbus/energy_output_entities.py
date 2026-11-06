"""The sensors of the inverter's energy management outputs, off by default."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory

from .coordinator import FroniusRuntimeData
from .descriptions import FroniusSensorDescription
from .energy_outputs import OUTPUT_PINS, OUTPUT_REASONS, OUTPUT_STATES, EnergyOutput


def _output(runtime: FroniusRuntimeData, pin: int) -> EnergyOutput | None:
    web_data = runtime.web_data
    if web_data is None or web_data.energy_outputs is None:
        return None
    return web_data.energy_outputs.get(pin)


def _state(runtime: FroniusRuntimeData, pin: int) -> str | None:
    output = _output(runtime, pin)
    return None if output is None else output.state


def _reason(runtime: FroniusRuntimeData, pin: int) -> str | None:
    output = _output(runtime, pin)
    return None if output is None else output.reason


def _description(
    key: str,
    options: list[str],
    value_fn: Callable[[FroniusRuntimeData, int], str | None],
    pin: int,
) -> FroniusSensorDescription:
    return FroniusSensorDescription(
        key=key,
        translation_key=key,
        device="inverter",
        source="web",
        web_client="public",
        value_fn=lambda runtime: value_fn(runtime, pin),
        device_class=SensorDeviceClass.ENUM,
        options=options,
        entity_category=EntityCategory.DIAGNOSTIC,
        # An output nobody configured would only ever say off.
        entity_registry_enabled_default=False,
    )


_DESCRIPTIONS = tuple(
    description
    for pin in OUTPUT_PINS
    for description in (
        _description(f"energy_output_{pin}", OUTPUT_STATES, _state, pin),
        _description(f"energy_output_{pin}_reason", OUTPUT_REASONS, _reason, pin),
    )
)


def energy_output_sensor_descriptions(
    runtime: FroniusRuntimeData,
) -> list[FroniusSensorDescription]:
    """The output sensors of this inverter.

    A 404 means firmware without the endpoint, but one at a restart must not
    delete sensors the owner has, so a registered one stays.
    """
    web_data = runtime.web_data
    if web_data is None or not web_data.energy_outputs_missing:
        return list(_DESCRIPTIONS)
    return [d for d in _DESCRIPTIONS if d.key in runtime.registered_keys]
