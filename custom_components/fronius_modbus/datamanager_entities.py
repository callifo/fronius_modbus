"""The sensors of a Datamanager's own registers and of its export limit."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

from .const import (
    CONTROL_STATUS,
    EXPORT_LIMIT_MODES,
    SENSOR_STATE_OPTIONS,
    UNKNOWN_STATE,
)
from .coordinator import FroniusRuntimeData
from .datamanager_client import ExportLimits
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


def _limits(runtime: FroniusRuntimeData) -> ExportLimits:
    """The export limits of the last poll; none before its first answer."""
    web = runtime.datamanager_web
    return (web.data if web is not None else None) or ExportLimits()


def _export_watts(
    key: str, value_fn: Callable[[ExportLimits], float | None]
) -> FroniusSensorDescription:
    return FroniusSensorDescription(
        key=key,
        translation_key=key,
        device="inverter",
        source="datamanager",
        value_fn=lambda runtime: value_fn(_limits(runtime)),
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="W",
        entity_category=EntityCategory.DIAGNOSTIC,
    )


def _export_state(
    key: str, value_fn: Callable[[ExportLimits], str | None]
) -> FroniusSensorDescription:
    return FroniusSensorDescription(
        key=key,
        translation_key=key,
        device="inverter",
        source="datamanager",
        value_fn=lambda runtime: value_fn(_limits(runtime)),
        device_class=SensorDeviceClass.ENUM,
        options=SENSOR_STATE_OPTIONS[key],
        entity_category=EntityCategory.DIAGNOSTIC,
    )


def _on_off(flag: bool | None) -> str | None:
    return None if flag is None else CONTROL_STATUS[int(flag)]


def _mode(limits: ExportLimits) -> str | None:
    if limits.mode is None:
        return None
    return EXPORT_LIMIT_MODES.get(limits.mode, UNKNOWN_STATE)


_EXPORT_LIMITS = (
    _export_watts("export_soft_limit", lambda limits: limits.soft_w),
    _export_state(
        "export_soft_limit_state", lambda limits: _on_off(limits.soft_enabled)
    ),
    _export_watts("export_hard_limit", lambda limits: limits.hard_w),
    _export_state(
        "export_hard_limit_state", lambda limits: _on_off(limits.hard_enabled)
    ),
    _export_state("export_limit_mode", _mode),
    _export_state("export_fail_safe", lambda limits: _on_off(limits.fail_safe)),
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
    if runtime.datamanager_web is not None:
        descriptions += _EXPORT_LIMITS
    return descriptions
