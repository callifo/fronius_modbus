"""Entity descriptions for every platform, and the base entity that reads them.

Each description carries a ``value_fn`` (and, for writable platforms, a
``set_fn``/``turn_on``/``turn_off``/``press``) closing over a
:class:`FroniusRuntimeData`. The `*_descriptions()` factories build the full
list for the current runtime -- including the per-meter and per-MPPT-module
rows, which only exist once the device reports them -- so a platform's
``async_setup_entry`` is just ``factory(runtime)`` fed into an entity class.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any, Literal, cast

from modbus_connection import ModbusError

from homeassistant.components.button import ButtonEntityDescription
from homeassistant.components.number import NumberEntityDescription, NumberMode
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntityDescription
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    AC_LIMIT_STATUS,
    API_BATTERY_MODE,
    CHARGE_GRID_STATUS,
    CHARGE_STATUS,
    CONNECTION_STATUS_CONDENSED,
    CONTROL_STATUS,
    DOMAIN,
    ECP_CONNECTION_STATUS,
    FRONIUS_INVERTER_STATUS,
    INVERTER_CONTROLS,
    INVERTER_EVENTS,
    INVERTER_STATUS,
    SENSOR_STATE_OPTIONS,
    STORAGE_CONTROL_MODE,
    STORAGE_EXT_CONTROL_MODE,
    UNKNOWN_STATE,
    bitmask_to_string,
    entity_prefix,
    instance_key,
    map_code,
)
from .coordinator import (
    FroniusConfigEntry,
    FroniusModbusCoordinator,
    FroniusRuntimeData,
    FroniusWebCoordinator,
    assume_present,
)
from .derived import TotalGuard
from .fronius_modbus_api.device import (
    REPORT_CONTROLS,
    REPORT_INVERTER,
    REPORT_MPPT,
    REPORT_NAMEPLATE,
    REPORT_SETTINGS,
    REPORT_STATUS,
    REPORT_STORAGE,
    meter_report_name,
)
from .fronius_modbus_api.storage import (
    CHARGE_LIMIT_MODES,
    DISCHARGE_LIMIT_MODES,
    ExtendedMode,
)
from .fronius_modbus_api.sunspec_models import MpptModule

_LOGGER = logging.getLogger(__name__)

type Source = Literal["modbus", "web"]
type WebClientKind = Literal["customer", "technician"]
type DeviceKind = Literal["inverter", "storage", "meter"]

# A new poll below the last value, or a jump above it, is a bad reading, not a reset.
TOTAL_INCREASING_MAX_STEP_WH = 100_000
# ... unless the lower value keeps coming: a meter or inverter swap really does
# restart the counter, and refusing it forever would freeze the sensor for good.
TOTAL_INCREASING_RESET_POLLS = 3
# 0.3's static fallback for the AC limit rate's max, kept when settings.w_max is unknown.
AC_LIMIT_RATE_FALLBACK_MAX_W = 50_000


@dataclass(frozen=True, kw_only=True)
class FroniusDescriptionMixin:
    """Fields every Fronius entity description carries, on top of the HA one."""

    key: str
    device: DeviceKind
    source: Source = "modbus"
    # Which web login a web-sourced entity needs; it goes unavailable without it.
    web_client: WebClientKind = "customer"
    report_name: str | None = None
    meter_unit_id: int | None = None
    value_fn: Callable[[FroniusRuntimeData], Any]
    exists_fn: Callable[[FroniusRuntimeData], bool] = staticmethod(lambda runtime: True)
    available_fn: Callable[[FroniusRuntimeData], bool] = staticmethod(
        lambda runtime: True
    )


@dataclass(frozen=True, kw_only=True)
class FroniusSensorDescription(SensorEntityDescription, FroniusDescriptionMixin):
    """A sensor built from a value_fn."""


@dataclass(frozen=True, kw_only=True)
class FroniusNumberDescription(NumberEntityDescription, FroniusDescriptionMixin):
    """A number built from a value_fn and a set_fn."""

    set_fn: Callable[[FroniusRuntimeData, float], Awaitable[None]]
    max_fn: Callable[[FroniusRuntimeData], float | None] = staticmethod(
        lambda runtime: None
    )


@dataclass(frozen=True, kw_only=True)
class FroniusSelectDescription(SelectEntityDescription, FroniusDescriptionMixin):
    """A select built from a code<->label map and a set_fn taking the code."""

    options_map: dict[int, str]
    set_fn: Callable[[FroniusRuntimeData, int], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class FroniusSwitchDescription(SwitchEntityDescription, FroniusDescriptionMixin):
    """A switch built from a value_fn and separate turn_on/turn_off actions."""

    turn_on: Callable[[FroniusRuntimeData], Awaitable[None]]
    turn_off: Callable[[FroniusRuntimeData], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class FroniusButtonDescription(ButtonEntityDescription, FroniusDescriptionMixin):
    """A button built from a press action."""

    press: Callable[[FroniusRuntimeData], Awaitable[None]]


type FroniusDescription = (
    FroniusSensorDescription
    | FroniusNumberDescription
    | FroniusSelectDescription
    | FroniusSwitchDescription
    | FroniusButtonDescription
)


# -- value helpers -----------------------------------------------------------------


def _enum_sensor_options(translation_key: str) -> list[str] | None:
    return SENSOR_STATE_OPTIONS.get(translation_key)


def _control_status(value: bool | None) -> str | None:
    """CONTROL_STATUS as the inverter-controls booleans report it: None stays None."""
    if value is None:
        return None
    return CONTROL_STATUS[1] if value else CONTROL_STATUS[0]


def _ac_limit_status(value: bool | None) -> str:
    """AC_LIMIT_STATUS, with an explicit UNKNOWN_STATE for a value the controls haven't read yet."""
    if value is None:
        return UNKNOWN_STATE
    return AC_LIMIT_STATUS[1] if value else AC_LIMIT_STATUS[0]


def _mppt_channel_module(
    runtime: FroniusRuntimeData, channel: int | None
) -> MpptModule:
    """The MPPT module on a storage channel, assumed present like its channel."""
    return assume_present(runtime.device.mppt_module(assume_present(channel)))


def _storage_present(runtime: FroniusRuntimeData) -> bool:
    return runtime.device.storage is not None


def _web_field(runtime: FroniusRuntimeData, field_name: str) -> Any:
    web_data = runtime.web_data
    return None if web_data is None else getattr(web_data, field_name)


def _web_client_present(runtime: FroniusRuntimeData, kind: WebClientKind) -> bool:
    control = runtime.web_control
    if control is None:
        return False
    return control.technician_configured if kind == "technician" else control.configured


def _web_configured(runtime: FroniusRuntimeData) -> bool:
    return runtime.web_control is not None and runtime.web_control.configured


def _controls_present(runtime: FroniusRuntimeData) -> bool:
    return runtime.inverter_controls is not None


def _controls_value(runtime: FroniusRuntimeData, getter: Callable[[Any], Any]) -> Any:
    """Read a value off inverter_controls, or None while it hasn't loaded yet."""
    controls = runtime.inverter_controls
    return None if controls is None else getter(controls)


# -- sensor table --------------------------------------------------------------


def _sensor(
    key: str,
    translation_key: str,
    *,
    device: DeviceKind = "inverter",
    source: Source = "modbus",
    web_client: WebClientKind = "customer",
    report_name: str | None = None,
    meter_unit_id: int | None = None,
    value_fn: Callable[[FroniusRuntimeData], Any],
    exists_fn: Callable[[FroniusRuntimeData], bool] = lambda runtime: True,
    available_fn: Callable[[FroniusRuntimeData], bool] = lambda runtime: True,
    device_class: SensorDeviceClass | None = None,
    state_class: SensorStateClass | None = None,
    unit: str | None = None,
    icon: str | None = None,
    entity_category: EntityCategory | None = None,
) -> FroniusSensorDescription:
    options = _enum_sensor_options(translation_key)
    return FroniusSensorDescription(
        key=key,
        translation_key=translation_key,
        device=device,
        source=source,
        web_client=web_client,
        report_name=report_name,
        meter_unit_id=meter_unit_id,
        value_fn=value_fn,
        exists_fn=exists_fn,
        available_fn=available_fn,
        device_class=SensorDeviceClass.ENUM if options is not None else device_class,
        state_class=state_class,
        native_unit_of_measurement=unit,
        icon=icon,
        entity_category=entity_category,
        options=options,
    )


_STATIC_SENSOR_DESCRIPTIONS: tuple[FroniusSensorDescription, ...] = (
    _sensor(
        "A",
        "ac_current",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).a,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        unit="A",
        icon="mdi:current-ac",
    ),
    _sensor(
        "AphA",
        "ac_current_l1",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).aph_a,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        unit="A",
        icon="mdi:current-ac",
    ),
    _sensor(
        "acpower",
        "acpower",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).w,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "var",
        "var",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).v_ar,
        state_class=SensorStateClass.MEASUREMENT,
        unit="var",
        icon="mdi:sine-wave",
    ),
    _sensor(
        "acenergy",
        "acenergy",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).wh,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unit="Wh",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "pv_power",
        "pv_power",
        report_name=REPORT_MPPT,
        value_fn=lambda r: r.device.pv_power_w,
        exists_fn=lambda r: r.device.mppt is not None,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        icon="mdi:solar-power",
    ),
    _sensor(
        "pv_connection",
        "pv_connection",
        report_name=REPORT_STATUS,
        value_fn=lambda r: map_code(
            CONNECTION_STATUS_CONDENSED, assume_present(r.device.status).pv_conn
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "ecp_connection",
        "ecp_connection",
        report_name=REPORT_STATUS,
        value_fn=lambda r: map_code(
            ECP_CONNECTION_STATUS, assume_present(r.device.status).ecp_conn
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "status",
        "status",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: map_code(
            INVERTER_STATUS, assume_present(r.device.inverter).st
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "statusvendor",
        "statusvendor",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: map_code(
            FRONIUS_INVERTER_STATUS, assume_present(r.device.inverter).st_vnd
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "line_frequency",
        "line_frequency",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).hz,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        unit="Hz",
    ),
    _sensor(
        "inverter_controls",
        "control_mode",
        report_name=REPORT_STATUS,
        value_fn=lambda r: bitmask_to_string(
            assume_present(r.device.status).st_act_ctl, INVERTER_CONTROLS, "normal"
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "vref",
        "vref",
        report_name=REPORT_SETTINGS,
        value_fn=lambda r: assume_present(r.device.settings).v_ref,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "vrefofs",
        "vrefofs",
        report_name=REPORT_SETTINGS,
        value_fn=lambda r: assume_present(r.device.settings).v_ref_ofs,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "max_power",
        "max_power",
        report_name=REPORT_SETTINGS,
        value_fn=lambda r: assume_present(r.device.settings).w_max,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "events2",
        "events2",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: bitmask_to_string(
            assume_present(r.device.inverter).evt_vnd2, INVERTER_EVENTS, "none", bits=32
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "Conn",
        "connection_control",
        report_name=REPORT_CONTROLS,
        value_fn=lambda r: _controls_value(r, lambda c: _control_status(c.connected)),
        exists_fn=_controls_present,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "WMaxLim_Ena",
        "power_limit_control",
        report_name=REPORT_CONTROLS,
        value_fn=lambda r: _controls_value(
            r, lambda c: _control_status(c.ac_limit_enabled)
        ),
        exists_fn=_controls_present,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "OutPFSet_Ena",
        "power_factor_control",
        report_name=REPORT_CONTROLS,
        value_fn=lambda r: _controls_value(
            r, lambda c: _control_status(c.power_factor_enabled)
        ),
        exists_fn=_controls_present,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "VArPct_Ena",
        "reactive_power_control",
        report_name=REPORT_CONTROLS,
        value_fn=lambda r: _controls_value(
            r, lambda c: _control_status(c.var_percent_enabled)
        ),
        exists_fn=_controls_present,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "PhVphA",
        "ac_voltage_l1_n",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).ph_vph_a,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "i_unit_id",
        "unit_id",
        value_fn=lambda r: assume_present(r.device.identity).address,
        exists_fn=lambda r: r.device.identity is not None,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "ac_limit_rate",
        "ac_limit_rate",
        report_name=REPORT_CONTROLS,
        value_fn=lambda r: _controls_value(r, lambda c: c.ac_limit_w),
        exists_fn=_controls_present,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        icon="mdi:chart-line",
    ),
    _sensor(
        "ac_limit_enable",
        "ac_limit_enable",
        report_name=REPORT_CONTROLS,
        value_fn=lambda r: _controls_value(
            r, lambda c: _ac_limit_status(c.ac_limit_enabled)
        ),
        exists_fn=_controls_present,
        icon="mdi:power-plug",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "isolation_resistance",
        "isolation_resistance",
        report_name=REPORT_STATUS,
        value_fn=lambda r: r.device.isolation_resistance_megaohm,
        state_class=SensorStateClass.MEASUREMENT,
        unit="MΩ",
        icon="mdi:omega",
    ),
    # -- three-phase-only inverter sensors -----------------------------------
    _sensor(
        "AphB",
        "ac_current_l2",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).aph_b,
        exists_fn=lambda r: r.device.three_phase,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        unit="A",
        icon="mdi:current-ac",
    ),
    _sensor(
        "AphC",
        "ac_current_l3",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).aph_c,
        exists_fn=lambda r: r.device.three_phase,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        unit="A",
        icon="mdi:current-ac",
    ),
    _sensor(
        "PhVphB",
        "ac_voltage_l2_n",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).ph_vph_b,
        exists_fn=lambda r: r.device.three_phase,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "PhVphC",
        "ac_voltage_l3_n",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).ph_vph_c,
        exists_fn=lambda r: r.device.three_phase,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "PPVphAB",
        "ac_voltage_l1_l2",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).pp_vph_ab,
        exists_fn=lambda r: r.device.three_phase,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "PPVphBC",
        "ac_voltage_l2_l3",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).pp_vph_bc,
        exists_fn=lambda r: r.device.three_phase,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "PPVphCA",
        "ac_voltage_l3_l1",
        report_name=REPORT_INVERTER,
        value_fn=lambda r: assume_present(r.device.inverter).pp_vph_ca,
        exists_fn=lambda r: r.device.three_phase,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
    ),
    # -- web sensors ------------------------------------------------------------
    _sensor(
        "inverter_temperature",
        "inverter_temperature",
        source="web",
        value_fn=lambda r: _web_field(r, "inverter_temperature"),
        exists_fn=_web_configured,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="°C",
        icon="mdi:thermometer",
    ),
    _sensor(
        "export_soft_limit",
        "export_soft_limit",
        source="web",
        web_client="technician",
        value_fn=lambda r: _web_field(r, "export_soft_limit_w"),
        exists_fn=_web_configured,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        icon="mdi:transmission-tower-export",
    ),
    _sensor(
        "api_modbus_mode",
        "api_modbus_mode",
        source="web",
        value_fn=lambda r: _web_field(r, "modbus_mode"),
        exists_fn=_web_configured,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "api_modbus_control",
        "api_modbus_control",
        source="web",
        value_fn=lambda r: _web_field(r, "modbus_control"),
        exists_fn=_web_configured,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "api_modbus_sunspec_mode",
        "api_modbus_sunspec_mode",
        source="web",
        value_fn=lambda r: _web_field(r, "sunspec_mode"),
        exists_fn=_web_configured,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "api_modbus_restriction",
        "api_modbus_restriction",
        source="web",
        value_fn=lambda r: _web_field(r, "modbus_restriction"),
        exists_fn=_web_configured,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "api_modbus_restriction_ip",
        "api_modbus_restriction_ip",
        source="web",
        value_fn=lambda r: _web_field(r, "modbus_restriction_ip"),
        exists_fn=_web_configured,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # -- storage sensors ----------------------------------------------------
    _sensor(
        "storage_charge_current",
        "storage_charge_current",
        report_name=REPORT_MPPT,
        value_fn=lambda r: _mppt_channel_module(r, r.device.mppt_channels.charge).dca,
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        unit="A",
        icon="mdi:current-dc",
    ),
    _sensor(
        "storage_charge_voltage",
        "storage_charge_voltage",
        report_name=REPORT_MPPT,
        value_fn=lambda r: _mppt_channel_module(r, r.device.mppt_channels.charge).dcv,
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "storage_charge_power",
        "storage_charge_power",
        report_name=REPORT_MPPT,
        value_fn=lambda r: _mppt_channel_module(r, r.device.mppt_channels.charge).dcw,
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        icon="mdi:home-battery",
    ),
    _sensor(
        "storage_charge_lfte",
        "storage_charge_lfte",
        report_name=REPORT_MPPT,
        value_fn=lambda r: _mppt_channel_module(r, r.device.mppt_channels.charge).dcwh,
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unit="Wh",
        icon="mdi:home-battery",
    ),
    _sensor(
        "storage_discharge_current",
        "storage_discharge_current",
        report_name=REPORT_MPPT,
        value_fn=lambda r: (
            _mppt_channel_module(r, r.device.mppt_channels.discharge).dca
        ),
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        unit="A",
        icon="mdi:current-dc",
    ),
    _sensor(
        "storage_discharge_voltage",
        "storage_discharge_voltage",
        report_name=REPORT_MPPT,
        value_fn=lambda r: (
            _mppt_channel_module(r, r.device.mppt_channels.discharge).dcv
        ),
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="V",
        icon="mdi:lightning-bolt",
    ),
    _sensor(
        "storage_discharge_power",
        "storage_discharge_power",
        report_name=REPORT_MPPT,
        value_fn=lambda r: (
            _mppt_channel_module(r, r.device.mppt_channels.discharge).dcw
        ),
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        icon="mdi:home-battery",
    ),
    _sensor(
        "storage_discharge_lfte",
        "storage_discharge_lfte",
        report_name=REPORT_MPPT,
        value_fn=lambda r: (
            _mppt_channel_module(r, r.device.mppt_channels.discharge).dcwh
        ),
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unit="Wh",
        icon="mdi:home-battery",
    ),
    _sensor(
        "storage_connection",
        "storage_connection",
        report_name=REPORT_STATUS,
        value_fn=lambda r: map_code(
            CONNECTION_STATUS_CONDENSED, assume_present(r.device.status).stor_conn
        ),
        exists_fn=_storage_present,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "storage_temperature",
        "storage_temperature",
        device="storage",
        source="web",
        value_fn=lambda r: _web_field(r, "storage_temperature"),
        exists_fn=lambda r: _storage_present(r) and _web_configured(r),
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        unit="°C",
        icon="mdi:thermometer",
    ),
    _sensor(
        "control_mode",
        "control_mode",
        device="storage",
        report_name=REPORT_STORAGE,
        value_fn=lambda r: map_code(
            STORAGE_CONTROL_MODE, assume_present(r.storage_control).control_mode
        ),
        exists_fn=_storage_present,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "charge_status",
        "charge_status",
        device="storage",
        report_name=REPORT_STORAGE,
        value_fn=lambda r: map_code(
            CHARGE_STATUS, assume_present(r.device.storage).cha_st
        ),
        exists_fn=_storage_present,
        # 0.3's row for this entity had no seventh field, so it carried no category.
        entity_category=None,
    ),
    _sensor(
        "max_charge",
        "max_charge",
        device="storage",
        report_name=REPORT_STORAGE,
        value_fn=lambda r: assume_present(r.device.storage).w_cha_max,
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "soc",
        "soc",
        device="storage",
        report_name=REPORT_STORAGE,
        value_fn=lambda r: assume_present(r.device.storage).cha_state,
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        unit="%",
    ),
    _sensor(
        "charging_power",
        "charging_power",
        device="storage",
        report_name=REPORT_STORAGE,
        value_fn=lambda r: assume_present(r.device.storage).in_w_rte,
        exists_fn=_storage_present,
        unit="%",
        icon="mdi:gauge",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "discharging_power",
        "discharging_power",
        device="storage",
        report_name=REPORT_STORAGE,
        value_fn=lambda r: assume_present(r.device.storage).out_w_rte,
        exists_fn=_storage_present,
        unit="%",
        icon="mdi:gauge",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "soc_minimum",
        "soc_minimum",
        device="storage",
        report_name=REPORT_STORAGE,
        value_fn=lambda r: assume_present(r.storage_control).soc_minimum,
        exists_fn=_storage_present,
        unit="%",
        icon="mdi:gauge",
    ),
    _sensor(
        "grid_charging",
        "grid_charging",
        device="storage",
        report_name=REPORT_STORAGE,
        value_fn=lambda r: map_code(
            CHARGE_GRID_STATUS, assume_present(r.device.storage).cha_gri_set
        ),
        exists_fn=_storage_present,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "WHRtg",
        "energy_rating",
        device="storage",
        report_name=REPORT_NAMEPLATE,
        value_fn=lambda r: assume_present(r.device.nameplate).wh_rtg,
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.ENERGY,
        unit="Wh",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "MaxChaRte",
        "max_charge_rate",
        device="storage",
        report_name=REPORT_NAMEPLATE,
        value_fn=lambda r: assume_present(r.device.nameplate).max_cha_rte,
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _sensor(
        "MaxDisChaRte",
        "max_discharge_rate",
        device="storage",
        report_name=REPORT_NAMEPLATE,
        value_fn=lambda r: assume_present(r.device.nameplate).max_dis_cha_rte,
        exists_fn=_storage_present,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit="W",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

_SINGLE_PHASE_UNSUPPORTED_METER_KEYS = (
    "AphB",
    "AphC",
    "WphB",
    "WphC",
    "PhVphB",
    "PhVphC",
    "PPV",
)


# The 0.3 data keys double as translation keys, but hassfest only accepts
# [a-z0-9-_]+ there; the unique ids keep the data keys, the names move.
_TRANSLATION_KEYS: dict[str, str] = {
    "A": "ac_current",
    "AphA": "ac_current_l1",
    "AphB": "ac_current_l2",
    "AphC": "ac_current_l3",
    "Conn": "connection_control",
    "WMaxLim_Ena": "power_limit_control",
    "OutPFSet_Ena": "power_factor_control",
    "VArPct_Ena": "reactive_power_control",
    "PhVphA": "ac_voltage_l1_n",
    "PhVphB": "ac_voltage_l2_n",
    "PhVphC": "ac_voltage_l3_n",
    "PPV": "ac_voltage_line_line",
    "PPVphAB": "ac_voltage_l1_l2",
    "PPVphBC": "ac_voltage_l2_l3",
    "PPVphCA": "ac_voltage_l3_l1",
    "WphA": "power_l1",
    "WphB": "power_l2",
    "WphC": "power_l3",
    "WHRtg": "energy_rating",
    "MaxChaRte": "max_charge_rate",
    "MaxDisChaRte": "max_discharge_rate",
}


_METER_SENSOR_SPECS: tuple[tuple, ...] = (
    (
        "A",
        lambda meter: meter.a,
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
        "A",
        "mdi:current-ac",
    ),
    (
        "AphA",
        lambda meter: meter.aph_a,
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
        "A",
        "mdi:current-ac",
    ),
    (
        "AphB",
        lambda meter: meter.aph_b,
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
        "A",
        "mdi:current-ac",
    ),
    (
        "AphC",
        lambda meter: meter.aph_c,
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
        "A",
        "mdi:current-ac",
    ),
    (
        "power",
        lambda meter: meter.w,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
        "W",
        "mdi:lightning-bolt",
    ),
    (
        "WphA",
        lambda meter: meter.wph_a,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
        "W",
        "mdi:lightning-bolt",
    ),
    (
        "WphB",
        lambda meter: meter.wph_b,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
        "W",
        "mdi:lightning-bolt",
    ),
    (
        "WphC",
        lambda meter: meter.wph_c,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
        "W",
        "mdi:lightning-bolt",
    ),
    (
        "exported",
        lambda meter: meter.tot_wh_exp,
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
        "Wh",
        "mdi:lightning-bolt",
    ),
    (
        "imported",
        lambda meter: meter.tot_wh_imp,
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
        "Wh",
        "mdi:lightning-bolt",
    ),
    (
        "line_frequency",
        lambda meter: meter.hz,
        SensorDeviceClass.FREQUENCY,
        SensorStateClass.MEASUREMENT,
        "Hz",
        None,
    ),
    (
        "PhVphA",
        lambda meter: meter.ph_vph_a,
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
        "V",
        "mdi:lightning-bolt",
    ),
    (
        "PhVphB",
        lambda meter: meter.ph_vph_b,
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
        "V",
        "mdi:lightning-bolt",
    ),
    (
        "PhVphC",
        lambda meter: meter.ph_vph_c,
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
        "V",
        "mdi:lightning-bolt",
    ),
    (
        "PPV",
        lambda meter: meter.ppv,
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
        "V",
        "mdi:lightning-bolt",
    ),
)


def _meter_value(
    unit_id: int, getter: Callable[[Any], Any]
) -> Callable[[FroniusRuntimeData], Any]:
    """A value_fn reading one field off a meter, bound to that meter's unit id."""
    return lambda runtime: getter(runtime.device.meters[unit_id].meter)


def _mppt_module_value(
    index: int, attribute: str
) -> Callable[[FroniusRuntimeData], Any]:
    """A value_fn reading one field off an MPPT module, bound to that module."""
    return lambda runtime: getattr(runtime.device.mppt_module(index), attribute)


def _meter_sensor_descriptions(
    unit_id: int, phases: int
) -> list[FroniusSensorDescription]:
    descriptions = [
        _sensor(
            f"meter_{unit_id}_{suffix}",
            _TRANSLATION_KEYS.get(suffix, suffix),
            device="meter",
            report_name=meter_report_name(unit_id),
            meter_unit_id=unit_id,
            value_fn=_meter_value(unit_id, getter),
            device_class=device_class,
            state_class=state_class,
            unit=unit,
            icon=icon,
        )
        for suffix, getter, device_class, state_class, unit, icon in _METER_SENSOR_SPECS
        if phases != 1 or suffix not in _SINGLE_PHASE_UNSUPPORTED_METER_KEYS
    ]
    descriptions.append(
        _sensor(
            f"meter_{unit_id}_unit_id",
            "unit_id",
            device="meter",
            report_name=meter_report_name(unit_id),
            meter_unit_id=unit_id,
            value_fn=lambda runtime: unit_id,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
    )
    return descriptions


_MPPT_MODULE_SENSOR_SPECS = (
    (
        "dc_current",
        "dca",
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
        "A",
        "mdi:current-dc",
    ),
    (
        "dc_voltage",
        "dcv",
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
        "V",
        "mdi:lightning-bolt",
    ),
    (
        "dc_power",
        "dcw",
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
        "W",
        "mdi:solar-power",
    ),
    (
        "lifetime_energy",
        "dcwh",
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
        "Wh",
        "mdi:solar-panel",
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
        icon,
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
                icon=icon,
            )
        )
    return descriptions


def _load_and_grid_status_descriptions(
    runtime: FroniusRuntimeData,
) -> list[FroniusSensorDescription]:
    unit_id = runtime.primary_meter_unit_id
    if unit_id not in runtime.device.meters:
        return []
    report_name = meter_report_name(unit_id)
    return [
        _sensor(
            "load",
            "load",
            report_name=report_name,
            value_fn=lambda r: r.modbus.data.load_w,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            unit="W",
            icon="mdi:lightning-bolt",
        ),
        _sensor(
            "grid_status",
            "grid_status",
            report_name=report_name,
            value_fn=lambda r: r.modbus.data.grid_status,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ]


def sensor_descriptions(runtime: FroniusRuntimeData) -> list[FroniusSensorDescription]:
    """Every sensor description the current runtime produces."""
    descriptions = [d for d in _STATIC_SENSOR_DESCRIPTIONS if d.exists_fn(runtime)]
    descriptions += _load_and_grid_status_descriptions(runtime)
    for unit_id, info in runtime.device.meters.items():
        descriptions += _meter_sensor_descriptions(unit_id, info.phases)
    for index in runtime.device.mppt_channels.pv:
        descriptions += _mppt_sensor_descriptions(index, runtime)
    return descriptions


# -- number table -----------------------------------------------------------------


def _storage_percent_number(
    key: str,
    *,
    max_key: str,
    percent_getter: Callable[[FroniusRuntimeData], float | None],
    set_fn: Callable[[FroniusRuntimeData, float], Awaitable[None]],
    available_fn: Callable[[FroniusRuntimeData], bool],
) -> FroniusNumberDescription:
    def value_fn(runtime: FroniusRuntimeData) -> float | None:
        percent = percent_getter(runtime)
        max_rate = getattr(runtime.storage_control, max_key)
        return None if percent is None else round(percent / 100.0 * max_rate, 0)

    return FroniusNumberDescription(
        key=key,
        translation_key=key,
        device="storage",
        report_name=REPORT_STORAGE,
        value_fn=value_fn,
        set_fn=set_fn,
        available_fn=available_fn,
        exists_fn=_storage_present,
        max_fn=lambda r: getattr(r.storage_control, max_key),
        native_min_value=0,
        native_max_value=10100,
        native_step=10,
        mode=NumberMode.BOX,
        native_unit_of_measurement="W",
    )


async def _set_soc_minimum(runtime: FroniusRuntimeData, value: float) -> None:
    """Write the SoC minimum to Modbus, then mirror it to the web API in Manual mode."""
    percent = int(round(value))
    web_control = runtime.web_control
    mirror_to_web = (
        web_control is not None
        and web_control.configured
        and web_control.battery_mode_is_manual
    )
    # A minimum the web API refuses must not reach Modbus either: the two would
    # otherwise disagree, with the reserve only half applied.
    if mirror_to_web:
        assume_present(web_control).validate_soc_minimum(percent)
    await assume_present(runtime.storage_control).set_minimum_reserve(percent)
    if mirror_to_web:
        await assume_present(web_control).set_soc_minimum_manual(percent)


_NUMBER_DESCRIPTIONS: tuple[FroniusNumberDescription, ...] = (
    _storage_percent_number(
        "grid_discharge_power",
        max_key="max_discharge_rate_w",
        percent_getter=lambda r: (
            assume_present(r.storage_control).grid_discharge_power_pct
        ),
        set_fn=lambda r, v: assume_present(
            r.storage_control
        ).set_grid_discharge_power_w(v),
        available_fn=lambda r: (
            assume_present(r.storage_control).grid_discharge_power_pct is not None
            and assume_present(r.storage_control).extended_mode
            is ExtendedMode.DISCHARGE_TO_GRID
        ),
    ),
    _storage_percent_number(
        "grid_charge_power",
        max_key="max_charge_rate_w",
        percent_getter=lambda r: (
            assume_present(r.storage_control).grid_charge_power_pct
        ),
        set_fn=lambda r, v: assume_present(r.storage_control).set_grid_charge_power_w(
            v
        ),
        available_fn=lambda r: (
            assume_present(r.storage_control).grid_charge_power_pct is not None
            and assume_present(r.storage_control).extended_mode
            is ExtendedMode.CHARGE_FROM_GRID
        ),
    ),
    _storage_percent_number(
        "discharge_limit",
        max_key="max_discharge_rate_w",
        percent_getter=lambda r: assume_present(r.storage_control).discharge_limit_pct,
        set_fn=lambda r, v: assume_present(r.storage_control).set_discharge_limit_w(v),
        available_fn=lambda r: (
            assume_present(r.storage_control).discharge_limit_pct is not None
            and assume_present(r.storage_control).extended_mode in DISCHARGE_LIMIT_MODES
        ),
    ),
    _storage_percent_number(
        "charge_limit",
        max_key="max_charge_rate_w",
        percent_getter=lambda r: assume_present(r.storage_control).charge_limit_pct,
        set_fn=lambda r, v: assume_present(r.storage_control).set_charge_limit_w(v),
        available_fn=lambda r: (
            assume_present(r.storage_control).charge_limit_pct is not None
            and assume_present(r.storage_control).extended_mode in CHARGE_LIMIT_MODES
        ),
    ),
    FroniusNumberDescription(
        key="soc_minimum",
        translation_key="soc_minimum",
        device="storage",
        report_name=REPORT_STORAGE,
        exists_fn=_storage_present,
        value_fn=lambda r: assume_present(r.storage_control).soc_minimum,
        set_fn=_set_soc_minimum,
        native_min_value=5,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        native_unit_of_measurement="%",
    ),
    FroniusNumberDescription(
        key="ac_limit_rate",
        translation_key="ac_limit_rate",
        device="inverter",
        report_name=REPORT_CONTROLS,
        value_fn=lambda r: _controls_value(r, lambda c: c.ac_limit_w),
        set_fn=lambda r, v: assume_present(r.inverter_controls).set_ac_limit_w(v),
        exists_fn=_controls_present,
        max_fn=lambda r: (
            (assume_present(r.device.settings).w_max if r.device.settings else None)
            or AC_LIMIT_RATE_FALLBACK_MAX_W
        ),
        native_min_value=0,
        native_max_value=AC_LIMIT_RATE_FALLBACK_MAX_W,
        native_step=10,
        mode=NumberMode.BOX,
        native_unit_of_measurement="W",
    ),
    FroniusNumberDescription(
        key="power_factor",
        translation_key="power_factor",
        device="inverter",
        report_name=REPORT_CONTROLS,
        value_fn=lambda r: _controls_value(r, lambda c: c.power_factor),
        set_fn=lambda r, v: assume_present(r.inverter_controls).set_power_factor(v),
        exists_fn=_controls_present,
        available_fn=lambda r: _controls_value(r, lambda c: c.power_factor) is not None,
        native_min_value=-1,
        native_max_value=1,
        native_step=0.001,
        mode=NumberMode.BOX,
    ),
    FroniusNumberDescription(
        key="api_battery_power",
        translation_key="api_battery_power",
        device="storage",
        source="web",
        value_fn=lambda r: _web_field(r, "battery_power_w"),
        set_fn=lambda r, v: assume_present(r.web_control).set_battery_power_w(v),
        exists_fn=lambda r: _storage_present(r) and _web_configured(r),
        available_fn=lambda r: (
            _web_configured(r) and assume_present(r.web_control).battery_mode_is_manual
        ),
        native_min_value=-20000,
        native_max_value=20000,
        native_step=10,
        mode=NumberMode.BOX,
        native_unit_of_measurement="W",
    ),
    FroniusNumberDescription(
        key="soc_maximum",
        translation_key="soc_maximum",
        device="storage",
        source="web",
        value_fn=lambda r: _web_field(r, "soc_max"),
        set_fn=lambda r, v: assume_present(r.web_control).set_soc_maximum(
            int(round(v))
        ),
        exists_fn=lambda r: _storage_present(r) and _web_configured(r),
        available_fn=lambda r: (
            _web_configured(r) and assume_present(r.web_control).battery_mode_is_manual
        ),
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        native_unit_of_measurement="%",
    ),
    FroniusNumberDescription(
        key="export_soft_limit",
        translation_key="export_soft_limit",
        web_client="technician",
        device="inverter",
        source="web",
        value_fn=lambda r: _web_field(r, "export_soft_limit_w"),
        set_fn=lambda r, v: assume_present(r.web_control).set_export_soft_limit_w(v),
        exists_fn=_web_configured,
        available_fn=lambda r: (
            r.web_control is not None
            and assume_present(r.web_control).technician_configured
        ),
        native_min_value=0,
        native_max_value=15000,
        native_step=10,
        mode=NumberMode.BOX,
        native_unit_of_measurement="W",
    ),
)


def number_descriptions(runtime: FroniusRuntimeData) -> list[FroniusNumberDescription]:
    """Every number description the current runtime produces."""
    return [d for d in _NUMBER_DESCRIPTIONS if d.exists_fn(runtime)]


# -- select table -----------------------------------------------------------------


async def _set_ext_control_mode(runtime: FroniusRuntimeData, code: int) -> None:
    mode = ExtendedMode(code)
    await assume_present(runtime.storage_control).set_mode(mode)
    web_control = runtime.web_control
    if (
        mode is ExtendedMode.CHARGE_FROM_GRID
        and web_control is not None
        and web_control.configured
    ):
        try:
            await web_control.set_charge_sources(
                charge_from_grid=True, charge_from_ac=True
            )
        except (ModbusError, RuntimeError, ValueError) as err:
            _LOGGER.warning(
                "Failed to enable charge from grid via the web API: %s", err
            )


_CONTROL_STATUS_OPTIONS = {0: CONTROL_STATUS[0], 1: CONTROL_STATUS[1]}


def _select(
    key: str,
    translation_key: str | None = None,
    *,
    device: DeviceKind = "inverter",
    source: Source = "modbus",
    options_map: dict[int, str],
    value_fn: Callable[[FroniusRuntimeData], str | None],
    set_fn: Callable[[FroniusRuntimeData, int], Awaitable[None]],
    exists_fn: Callable[[FroniusRuntimeData], bool] = lambda runtime: True,
    available_fn: Callable[[FroniusRuntimeData], bool] = lambda runtime: True,
    report_name: str | None = None,
) -> FroniusSelectDescription:
    return FroniusSelectDescription(
        key=key,
        translation_key=translation_key or key,
        device=device,
        source=source,
        report_name=report_name,
        options_map=options_map,
        value_fn=value_fn,
        set_fn=set_fn,
        exists_fn=exists_fn,
        available_fn=available_fn,
        options=list(options_map.values()),
    )


_SELECT_DESCRIPTIONS: tuple[FroniusSelectDescription, ...] = (
    _select(
        "ext_control_mode",
        device="storage",
        report_name=REPORT_STORAGE,
        options_map=STORAGE_EXT_CONTROL_MODE,
        value_fn=lambda r: map_code(
            STORAGE_EXT_CONTROL_MODE,
            assume_present(r.storage_control).extended_mode.value,
        ),
        set_fn=_set_ext_control_mode,
        exists_fn=_storage_present,
    ),
    _select(
        "api_battery_mode",
        device="storage",
        source="web",
        options_map=API_BATTERY_MODE,
        value_fn=lambda r: _web_field(r, "battery_mode"),
        set_fn=lambda r, code: assume_present(r.web_control).set_battery_mode(code),
        exists_fn=lambda r: _storage_present(r) and _web_configured(r),
    ),
    _select(
        "ac_limit_enable",
        report_name=REPORT_CONTROLS,
        options_map=_CONTROL_STATUS_OPTIONS,
        value_fn=lambda r: _controls_value(
            r, lambda c: _control_status(c.ac_limit_enabled)
        ),
        set_fn=lambda r, code: assume_present(r.inverter_controls).set_ac_limit_enable(
            bool(code)
        ),
        exists_fn=_controls_present,
    ),
    _select(
        "power_factor_enable",
        report_name=REPORT_CONTROLS,
        options_map=_CONTROL_STATUS_OPTIONS,
        value_fn=lambda r: _controls_value(
            r, lambda c: _control_status(c.power_factor_enabled)
        ),
        set_fn=lambda r, code: assume_present(
            r.inverter_controls
        ).set_power_factor_enable(bool(code)),
        exists_fn=_controls_present,
        available_fn=lambda r: (
            _controls_value(r, lambda c: c.power_factor_enabled) is not None
        ),
    ),
    _select(
        "Conn",
        translation_key=_TRANSLATION_KEYS["Conn"],
        report_name=REPORT_CONTROLS,
        options_map=_CONTROL_STATUS_OPTIONS,
        value_fn=lambda r: _controls_value(r, lambda c: _control_status(c.connected)),
        set_fn=lambda r, code: assume_present(r.inverter_controls).set_connected(
            bool(code)
        ),
        exists_fn=_controls_present,
    ),
)


def select_descriptions(runtime: FroniusRuntimeData) -> list[FroniusSelectDescription]:
    """Every select description the current runtime produces."""
    return [d for d in _SELECT_DESCRIPTIONS if d.exists_fn(runtime)]


# -- switch table -----------------------------------------------------------------


def _switch(
    key: str,
    *,
    device: DeviceKind = "inverter",
    value_fn: Callable[[FroniusRuntimeData], bool | None],
    turn_on: Callable[[FroniusRuntimeData], Awaitable[None]],
    turn_off: Callable[[FroniusRuntimeData], Awaitable[None]],
    exists_fn: Callable[[FroniusRuntimeData], bool],
    icon: str | None = None,
    entity_category: EntityCategory | None = None,
) -> FroniusSwitchDescription:
    return FroniusSwitchDescription(
        key=key,
        translation_key=key,
        device=device,
        source="web",
        value_fn=value_fn,
        turn_on=turn_on,
        turn_off=turn_off,
        exists_fn=exists_fn,
        icon=icon,
        entity_category=entity_category,
    )


_SWITCH_DESCRIPTIONS: tuple[FroniusSwitchDescription, ...] = (
    _switch(
        "api_charge_from_ac",
        device="storage",
        value_fn=lambda r: _web_field(r, "charge_from_ac"),
        turn_on=lambda r: assume_present(r.web_control).set_charge_sources(
            charge_from_ac=True
        ),
        turn_off=lambda r: assume_present(r.web_control).set_charge_sources(
            charge_from_grid=False, charge_from_ac=False
        ),
        exists_fn=lambda r: _storage_present(r) and _web_configured(r),
        icon="mdi:power-plug-battery",
    ),
    _switch(
        "api_charge_from_grid",
        device="storage",
        value_fn=lambda r: _web_field(r, "charge_from_grid"),
        turn_on=lambda r: assume_present(r.web_control).set_charge_sources(
            charge_from_grid=True, charge_from_ac=True
        ),
        turn_off=lambda r: assume_present(r.web_control).set_charge_sources(
            charge_from_grid=False
        ),
        exists_fn=lambda r: _storage_present(r) and _web_configured(r),
        icon="mdi:transmission-tower-export",
    ),
    _switch(
        "api_solar_api_enabled",
        value_fn=lambda r: _web_field(r, "solar_api_enabled"),
        turn_on=lambda r: assume_present(r.web_control).set_solar_api_enabled(True),
        turn_off=lambda r: assume_present(r.web_control).set_solar_api_enabled(False),
        exists_fn=_web_configured,
        icon="mdi:api",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


def switch_descriptions(runtime: FroniusRuntimeData) -> list[FroniusSwitchDescription]:
    """Every switch description the current runtime produces."""
    return [d for d in _SWITCH_DESCRIPTIONS if d.exists_fn(runtime)]


# -- button table -----------------------------------------------------------------


_BUTTON_DESCRIPTIONS: tuple[FroniusButtonDescription, ...] = (
    FroniusButtonDescription(
        key="reset_modbus_control",
        translation_key="reset_modbus_control",
        device="inverter",
        source="web",
        value_fn=lambda r: None,
        press=lambda r: assume_present(r.web_control).reset_modbus_control(),
        exists_fn=_web_configured,
        icon="mdi:restart",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


def button_descriptions(runtime: FroniusRuntimeData) -> list[FroniusButtonDescription]:
    """Every button description the current runtime produces."""
    return [d for d in _BUTTON_DESCRIPTIONS if d.exists_fn(runtime)]


def expected_unique_ids(
    entry: FroniusConfigEntry, runtime: FroniusRuntimeData
) -> set[str]:
    """Every unique id the current runtime is expected to register, across platforms."""
    prefix = entity_prefix(entry.entry_id)
    factories = (
        sensor_descriptions,
        number_descriptions,
        select_descriptions,
        switch_descriptions,
        button_descriptions,
    )
    return {
        f"{prefix}_{description.key}"
        for factory in factories
        for description in factory(runtime)
    }


# -- device info and the base entity -----------------------------------------------


def device_info(
    runtime: FroniusRuntimeData,
    entry: FroniusConfigEntry,
    kind: DeviceKind,
    meter_unit_id: int | None = None,
) -> DeviceInfo:
    """The DeviceInfo for one of the entry's devices: the inverter, the battery, a meter."""
    key = instance_key(entry.entry_id)
    if kind == "inverter":
        identity = assume_present(runtime.device.identity)
        return DeviceInfo(
            identifiers={(DOMAIN, f"{key}_inverter")},
            name=f"Fronius {identity.model}",
            manufacturer=identity.manufacturer,
            model=identity.model,
            serial_number=identity.serial,
            sw_version=identity.version,
        )
    if kind == "storage":
        web_data = runtime.web_data
        storage_model = web_data.storage_model if web_data else None
        return DeviceInfo(
            identifiers={(DOMAIN, f"{key}_battery_storage")},
            name=storage_model or "Battery Storage",
            manufacturer=web_data.storage_manufacturer if web_data else None,
            model=storage_model,
            serial_number=web_data.storage_serial if web_data else None,
        )
    unit_id = assume_present(meter_unit_id)
    info = runtime.device.meters[unit_id]
    # The configured order, not the order the present meters happen to be in.
    position = runtime.device.meter_unit_ids.index(unit_id) + 1
    return DeviceInfo(
        identifiers={(DOMAIN, f"{key}_meter_{unit_id}")},
        name=f"Fronius {info.identity.model} Meter {position}",
        manufacturer=info.identity.manufacturer,
        model=info.identity.model,
        serial_number=info.identity.serial,
        sw_version=info.identity.version,
    )


class FroniusEntity(
    CoordinatorEntity[FroniusModbusCoordinator | FroniusWebCoordinator]
):
    """The entity every platform builds: a description read against the runtime."""

    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: FroniusRuntimeData,
        entry: FroniusConfigEntry,
        description: FroniusDescription,
    ) -> None:
        """Bind to the coordinator the description's source picks."""
        coordinator = (
            runtime.modbus
            if description.source == "modbus"
            else assume_present(runtime.web)
        )
        super().__init__(coordinator)
        self._runtime = runtime
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        if description.translation_placeholders is not None:
            self._attr_translation_placeholders = description.translation_placeholders
        self._attr_unique_id = f"{entity_prefix(entry.entry_id)}_{description.key}"
        self._attr_device_info = device_info(
            runtime, entry, description.device, description.meter_unit_id
        )

    @property
    def available(self) -> bool:
        """Whether the report backing this entity was refreshed, and available_fn agrees."""
        # Home Assistant types the attribute as a plain EntityDescription, and
        # a narrower annotation here collides with the platform base classes.
        description = cast(FroniusDescription, self.entity_description)
        if description.source == "modbus":
            coordinator = self._runtime.modbus
            if not coordinator.last_update_success:
                return False
            if (
                description.report_name is not None
                and description.report_name not in coordinator.data.report.updated
            ):
                return False
            return description.available_fn(self._runtime)
        web_coordinator = self._runtime.web
        # A rejected login clears the client but the coordinator keeps
        # succeeding on what is left (audit F16): the entities of that login
        # must not stay operable.
        if not _web_client_present(self._runtime, description.web_client):
            return False
        if web_coordinator is None or not web_coordinator.last_update_success:
            return False
        return description.available_fn(self._runtime)

    async def async_run_write(self, action: Callable[[], Awaitable[None]]) -> None:
        """Await a write action, mapping its errors to the ones HA expects."""
        try:
            await action()
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except (ModbusError, RuntimeError) as err:
            raise HomeAssistantError(str(err)) from err


class FroniusTotalSensor(FroniusEntity, RestoreSensor):
    """A monotonically-increasing sensor: always available, restores across restarts."""

    def __init__(
        self,
        runtime: FroniusRuntimeData,
        entry: FroniusConfigEntry,
        description: FroniusDescription,
    ) -> None:
        """Hand the acceptance policy to a TotalGuard fed once per poll."""
        super().__init__(runtime, entry, description)
        self._guard = TotalGuard(
            max_step=TOTAL_INCREASING_MAX_STEP_WH,
            confirmations=TOTAL_INCREASING_RESET_POLLS,
        )
        # The coordinator has polled before any entity exists: judge that poll
        # now so the first state is never empty.
        self._observe_poll()

    async def async_added_to_hass(self) -> None:
        """Seed the guard from the restored state, then judge the current poll."""
        await super().async_added_to_hass()
        last_data = await self.async_get_last_sensor_data()
        if last_data is not None:
            self._guard.seed(cast(float | None, last_data.native_value))
        self._observe_poll()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._observe_poll()
        super()._handle_coordinator_update()

    def _observe_poll(self) -> None:
        description = cast(FroniusDescription, self.entity_description)
        verdict = self._guard.observe(description.value_fn(self._runtime))
        if verdict is not None:
            _LOGGER.warning("%s: %s", self.entity_id, verdict)

    @property
    def available(self) -> bool:
        """A total sensor always shows its last accepted value."""
        return True

    @property
    def native_value(self) -> float | None:
        """The last value the guard accepted; reading it changes nothing."""
        return self._guard.value
