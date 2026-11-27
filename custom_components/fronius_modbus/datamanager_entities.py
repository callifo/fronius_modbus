"""The sensors of a Datamanager's own registers."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

from .coordinator import FroniusRuntimeData
from .descriptions import FroniusSensorDescription
from .fronius_modbus_api.device import REPORT_SITE, REPORT_STATE_CODE
from .fronius_modbus_api.fronius_registers import SiteTotals


def _site_sensor(
    runtime: FroniusRuntimeData,
    key: str,
    value_fn: Callable[[SiteTotals], int | None],
    *,
    device_class: SensorDeviceClass = SensorDeviceClass.ENERGY,
    state_class: SensorStateClass | None = None,
    unit: str = "Wh",
) -> FroniusSensorDescription:
    def _value(runtime: FroniusRuntimeData) -> int | None:
        site = runtime.device.site
        return None if site is None else value_fn(site)

    return FroniusSensorDescription(
        key=key,
        translation_key=key,
        device="inverter",
        report_name=REPORT_SITE,
        value_fn=_value,
        device_class=device_class,
        # Energies without one: nobody has seen whether a sleeping inverter
        # leaves the sum, and a dip would count its whole energy once more.
        state_class=state_class,
        native_unit_of_measurement=unit,
        # With one inverter they repeat what it reports itself.
        entity_registry_enabled_default=runtime.several_inverters,
    )


def _state_code(runtime: FroniusRuntimeData) -> int | None:
    state_code = runtime.device.state_code
    return None if state_code is None else state_code.code


_STATE_CODE = FroniusSensorDescription(
    key="state_code",
    translation_key="state_code",
    device="inverter",
    report_name=REPORT_STATE_CODE,
    value_fn=_state_code,
    entity_category=EntityCategory.DIAGNOSTIC,
)


def datamanager_sensor_descriptions(
    runtime: FroniusRuntimeData,
) -> list[FroniusSensorDescription]:
    """The sensors of the blocks this device answers."""
    descriptions: list[FroniusSensorDescription] = []
    if runtime.device.state_code is not None:
        descriptions.append(_STATE_CODE)
    if runtime.device.site is not None:
        descriptions += [
            _site_sensor(
                runtime,
                "site_power",
                lambda site: site.power,
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
                unit="W",
            ),
            _site_sensor(runtime, "site_energy_day", lambda site: site.energy_day),
            _site_sensor(runtime, "site_energy_year", lambda site: site.energy_year),
            _site_sensor(runtime, "site_energy_total", lambda site: site.energy_total),
        ]
    return descriptions
