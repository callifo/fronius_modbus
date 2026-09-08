"""Fronius Modbus Hub."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from functools import wraps
from typing import Any
from importlib.metadata import version
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from packaging import version as pkg_version

from .froniusmodbusclient import FroniusModbusClient
from .froniuswebclient import FroniusWebAuthError, FroniusWebClient

from .const import (
    API_BATTERY_MODE,
    API_SOC_MODE,
    DOMAIN,
    ENTITY_PREFIX,
    API_USERNAME,
    TECHNICIAN_USERNAME,
    SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
)
from .token_store import async_get_token_store

_LOGGER = logging.getLogger(__name__)
_SOLAR_API_WARNING_TRANSLATION_KEY = "solar_api_low_firmware"
_SOLAR_API_MINIMUM_VERSION = (1, 40, 7, 1)
_SOLAR_API_MINIMUM_VERSION_TEXT = "1.40.7-1"
_SOLAR_API_FIRMWARE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(\d+))?$")


def _serialized_write(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        async with self._write_lock:
            return await func(self, *args, **kwargs)

    return wrapper


def _export_limit_summary(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict) or not config:
        return {"available": False}

    export_limits = config.get("exportLimits", {})
    active_power = export_limits.get("activePower", {}) if isinstance(export_limits, dict) else {}
    soft_limit = active_power.get("softLimit", {}) if isinstance(active_power, dict) else {}
    hard_limit = active_power.get("hardLimit", {}) if isinstance(active_power, dict) else {}

    return {
        "available": True,
        "active_power_activated": active_power.get("activated") if isinstance(active_power, dict) else None,
        "soft_limit_enabled": soft_limit.get("enabled") if isinstance(soft_limit, dict) else None,
        "soft_limit_w": soft_limit.get("powerLimit") if isinstance(soft_limit, dict) else None,
        "hard_limit_enabled": hard_limit.get("enabled") if isinstance(hard_limit, dict) else None,
        "hard_limit_w": hard_limit.get("powerLimit") if isinstance(hard_limit, dict) else None,
        "fail_safe_enabled": export_limits.get("failSafeModeEnabled") if isinstance(export_limits, dict) else None,
    }

WEB_API_DATA_KEYS = (
    "inverter_temperature",
    "api_modbus_mode",
    "api_modbus_control",
    "api_modbus_sunspec_mode",
    "api_modbus_restriction",
    "api_modbus_restriction_ip",
    "api_solar_api_enabled",
    "storage_temperature",
    "api_battery_mode_raw",
    "api_battery_mode_effective_raw",
    "api_battery_mode_consistent",
    "api_battery_mode",
    "api_battery_power",
    "api_soc_mode_raw",
    "api_soc_mode",
    "api_soc_min",
    "soc_maximum",
    "api_backup_reserved",
    "api_charge_from_ac",
    "api_charge_from_grid",
    "export_soft_limit",
)

INVERTER_STATUS_DATA_KEYS = (
    "pv_connection",
    "storage_connection",
    "ecp_connection",
    "inverter_controls",
    "isolation_resistance",
)
INVERTER_SETTINGS_DATA_KEYS = (
    "max_power",
    "vref",
    "vrefofs",
)
INVERTER_CONTROL_DATA_KEYS = (
    "Conn",
    "WMaxLim_Ena",
    "OutPFSet_Ena",
    "power_factor_enable",
    "VArPct_Ena",
    "ac_limit_rate_sf",
    "power_factor_sf",
    "power_factor",
)
AC_LIMIT_DATA_KEYS = (
    "ac_limit_rate_raw",
    "ac_limit_rate_pct",
    "ac_limit_rate",
    "ac_limit_enable",
)
METER_DATA_KEY_SUFFIXES = (
    "A",
    "AphA",
    "AphB",
    "AphC",
    "PhVphA",
    "PhVphB",
    "PhVphC",
    "PPV",
    "WphA",
    "WphB",
    "WphC",
    "exported",
    "imported",
    "line_frequency",
    "power",
)
STORAGE_DATA_KEYS = (
    "grid_charging",
    "charge_status",
    "soc_minimum",
    "discharging_power",
    "charging_power",
    "soc",
    "max_charge",
    "WChaGra",
    "WDisChaGra",
    "discharge_limit",
    "grid_charge_power",
    "charge_limit",
    "grid_discharge_power",
    "control_mode",
    "ext_control_mode",
)

BATTERY_WRITE_MODBUS_RECOVERY_SECONDS = 30.0
BATTERY_WRITE_WEB_REFRESH_DELAY_SECONDS = 10.0
# Load is derived from separate inverter/meter polls, so keep a small skew guard.
LOAD_MAX_SAMPLE_SKEW_SECONDS = 0.5
# Fronius occasionally drops inverter AC power to ~0 for one poll while PV is clearly active.
LOAD_GLITCH_MIN_EXPORT_W = 1000.0
LOAD_GLITCH_MAX_ABS_INVERTER_W = 50.0
LOAD_GLITCH_MIN_PV_W = 1000.0
# During strong battery charging on Verto, meter export can exceed inverter AC power and
# the simple grid-meter formula produces bogus negative/zero load values.
LOAD_STORAGE_CHARGE_MIN_W = 1000.0


class FroniusCoordinator(DataUpdateCoordinator):
    """Coordinator for Fronius Modbus data updates."""

    def __init__(self, hass: HomeAssistant, hub: Hub, config_entry: ConfigEntry | None = None) -> None:
        """Initialize the coordinator."""
        if config_entry is not None:
            try:
                super().__init__(
                    hass,
                    _LOGGER,
                    name=f"{DOMAIN}_{hub._id}_coordinator",
                    update_interval=hub._scan_interval,
                    config_entry=config_entry,
                )
            except TypeError:
                super().__init__(
                    hass,
                    _LOGGER,
                    name=f"{DOMAIN}_{hub._id}_coordinator",
                    update_interval=hub._scan_interval,
                )
        else:
            super().__init__(
                hass,
                _LOGGER,
                name=f"{DOMAIN}_{hub._id}_coordinator",
                update_interval=hub._scan_interval,
            )
        self.hub = hub

    async def _async_update_data(self) -> dict:
        """Fetch all data from Fronius device."""
        core_err: Exception | None = None
        self.hub.data["load"] = None
        self.hub._client.start_load_poll_cycle()
        try:
            core_ok = await self.hub._client.read_inverter_data()
            if not core_ok:
                core_err = RuntimeError("Core inverter read returned no data")
        except Exception as err:
            core_err = err

        if core_err is not None:
            if self.hub._handle_core_modbus_failure(core_err):
                return self.hub.data
            raise UpdateFailed(f"Fronius data update failed: {core_err}")

        self.hub._handle_core_modbus_success()
        await self.hub._async_refresh_optional_data()
        return self.hub.data


class Hub:
    """Hub for Fronius Battery Storage Modbus Interface"""

    PYMODBUS_VERSION = '3.11.2'

    @staticmethod
    def _normalize_instance_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "fronius"

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        host: str,
        port: int,
        inverter_unit_id: int,
        meter_unit_ids,
        scan_interval: int,
        api_username: str | None = None,
        api_password: str | None = None,
        api_token: dict[str, str] | None = None,
        auto_enable_modbus: bool = True,
        restrict_modbus_to_this_ip: bool = False,
    ) -> None:
        """Init hub."""
        self._hass = hass
        self._name = name
        self._host = host
        self._port = port
        self._inverter_unit_id = inverter_unit_id
        self._meter_unit_ids = list(meter_unit_ids)
        self._fallback_instance_key = self._normalize_instance_key(host)
        self._config_entry: ConfigEntry | None = None
        self._auto_enable_modbus = auto_enable_modbus
        self._restrict_modbus_to_this_ip = restrict_modbus_to_this_ip
        self._api_username = api_username
        self._webclient: FroniusWebClient | None = None

        self._id = f'{name.lower()}_{host.lower().replace('.','')}'
        self.online = True

        self._client = FroniusModbusClient(host=host, port=port, inverter_unit_id=inverter_unit_id, meter_unit_ids=meter_unit_ids, timeout=max(3, (scan_interval - 1)))
        if api_username and (api_password or api_token):
            self._webclient = FroniusWebClient(
                host=host,
                username=api_username,
                password=api_password or "",
                token=api_token,
            )
        self._scan_interval = timedelta(seconds=scan_interval)
        self.coordinator = None
        self._write_lock = asyncio.Lock()
        self._battery_write_transition_until = 0.0
        self._battery_write_transition_warned = False
        self._delayed_web_refresh_task: asyncio.Task | None = None
        self._last_export_limit_summary: dict[str, Any] | None = None
        self._last_good_load_w: float | None = None
        self._last_good_inverter_power_w: float | None = None
        self._consecutive_bad_load_polls = 0

    def _meter_prefix(self, unit_id: int) -> str:
        return f"meter_{int(unit_id)}_"

    def _reset_bad_load_tracking(self) -> None:
        self._consecutive_bad_load_polls = 0

    def _set_load(
        self,
        load_w: float,
        *,
        cache: bool = False,
        inverter_power: float | None = None,
    ) -> None:
        load_value = round(float(load_w), 2)
        self.data["load"] = load_value
        if cache:
            self._last_good_load_w = load_value
            if inverter_power is not None:
                self._last_good_inverter_power_w = float(inverter_power)
        self._reset_bad_load_tracking()

    def _is_inverter_power_glitch(
        self,
        *,
        candidate_load: float,
        meter_power: float,
        inverter_power: float,
    ) -> bool:
        pv_power = self.data.get("pv_power")
        pv_active = self._client.is_numeric(pv_power) and float(pv_power) >= LOAD_GLITCH_MIN_PV_W
        previous_inverter_active = self._last_good_inverter_power_w is not None and (
            self._last_good_inverter_power_w >= LOAD_GLITCH_MIN_PV_W
        )
        return (
            candidate_load < 0
            and meter_power <= -LOAD_GLITCH_MIN_EXPORT_W
            and abs(inverter_power) <= LOAD_GLITCH_MAX_ABS_INVERTER_W
            and (pv_active or previous_inverter_active)
        )

    def _apply_glitch_load_fallback(self) -> None:
        self._consecutive_bad_load_polls += 1
        # Keep one last-good value for a single bad poll, then fall back to unavailable.
        if self._consecutive_bad_load_polls == 1 and self._last_good_load_w is not None:
            self.data["load"] = self._last_good_load_w

    def _solar_api_warning_issue_id(self) -> str | None:
        if self._config_entry is None:
            return None
        return f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}{self._config_entry.entry_id}"

    def _parse_firmware_version(self, version_text: Any) -> tuple[int, int, int, int] | None:
        if not isinstance(version_text, str):
            return None

        match = _SOLAR_API_FIRMWARE_RE.fullmatch(version_text.strip())
        if match is None:
            return None

        major, minor, patch, build = match.groups(default="0")
        return (int(major), int(minor), int(patch), int(build))

    def _solar_api_warning_needed(self) -> bool:
        if not self.web_api_configured:
            return False

        firmware_version = self._parse_firmware_version(self._client.data.get("i_sw_version"))
        if firmware_version is None:
            return False

        solar_api_enabled = self._client.data.get("api_solar_api_enabled")
        if solar_api_enabled is not True:
            return False

        return firmware_version < _SOLAR_API_MINIMUM_VERSION

    async def _async_sync_solar_api_warning(self) -> None:
        issue_id = self._solar_api_warning_issue_id()
        if issue_id is None:
            return

        if not self._solar_api_warning_needed():
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return

        current_version = self._client.data.get("i_sw_version")
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_SOLAR_API_WARNING_TRANSLATION_KEY,
            translation_placeholders={
                "entry_title": self._config_entry.title if self._config_entry is not None else self._name,
                "current_version": str(current_version),
                "minimum_version": _SOLAR_API_MINIMUM_VERSION_TEXT,
            },
            data={
                "entry_id": self._config_entry.entry_id,
                "current_version": str(current_version),
                "minimum_version": _SOLAR_API_MINIMUM_VERSION_TEXT,
            },
        )

    async def init_data(
        self,
        config_entry: ConfigEntry | None = None,
        setup_coordinator: bool = True,
        apply_modbus_config: bool = False,
    ):
        """Initialize data and coordinator."""
        self._config_entry = config_entry
        await self._hass.async_add_executor_job(self.check_pymodbus_version)
        meter_phase_counts: dict[int, int] = {}
        meter_locations: dict[int, int] = {}
        if self.web_api_configured:
            meter_info = await self._async_web_job(
                self._webclient.get_power_meter_info,
                self._client.primary_meter_unit_id,
            )
            if meter_info is None:
                _LOGGER.debug(
                    "Keeping existing smart meter unit ids for %s because PowerMeter payload parsing failed",
                    self._host,
                )
            elif isinstance(meter_info, dict) and meter_info.get("unit_ids"):
                self._client.set_meter_unit_ids(
                    meter_info.get("unit_ids"),
                    primary_unit_id=meter_info.get("primary_unit_id"),
                )
                phase_counts_by_unit_id = meter_info.get("phase_counts_by_unit_id")
                if isinstance(phase_counts_by_unit_id, dict):
                    for raw_unit_id, raw_phase_count in phase_counts_by_unit_id.items():
                        if (
                            not self._client.is_numeric(raw_unit_id)
                            or not self._client.is_numeric(raw_phase_count)
                        ):
                            continue
                        unit_id = int(raw_unit_id)
                        phase_count = int(raw_phase_count)
                        if unit_id <= 0 or phase_count <= 0:
                            continue
                        meter_phase_counts[unit_id] = phase_count
                locations_by_unit_id = meter_info.get("locations_by_unit_id")
                if isinstance(locations_by_unit_id, dict):
                    for raw_unit_id, raw_location in locations_by_unit_id.items():
                        if (
                            not self._client.is_numeric(raw_unit_id)
                            or not self._client.is_numeric(raw_location)
                        ):
                            continue
                        unit_id = int(raw_unit_id)
                        location = int(raw_location)
                        if unit_id <= 0 or location < 0:
                            continue
                        meter_locations[unit_id] = location
            else:
                _LOGGER.debug(
                    "Keeping existing smart meter unit ids for %s because PowerMeter payload contained no usable meters",
                    self._host,
                )
        if apply_modbus_config and self.web_api_configured and self._auto_enable_modbus:
            enabled = await self._async_web_job(
                self._webclient.ensure_modbus_enabled,
                self._port,
                self._client.primary_meter_unit_id,
                self._inverter_unit_id,
                self._restrict_modbus_to_this_ip,
            )
            if enabled:
                await asyncio.sleep(1.0)
        await self._client.init_data()
        for unit_id in self._client._meter_unit_ids:
            phase_count = meter_phase_counts.get(unit_id)
            if phase_count is not None:
                self.data[f"{self._meter_prefix(unit_id)}phase_count"] = phase_count
            location = meter_locations.get(unit_id)
            if location is not None:
                self.data[f"{self._meter_prefix(unit_id)}location"] = location
        if self._client.meter_configured and self._client.primary_meter_unit_id not in self._client._meter_unit_ids:
            _LOGGER.warning(
                "Configured meter unit ids %s do not include the primary meter unit id %s; Load and Grid status will stay unavailable",
                self._client._meter_unit_ids,
                self._client.primary_meter_unit_id,
            )

        if self.storage_configured:
            self._client.reset_storage_info()
            if self.web_api_configured:
                storage_info = await self._async_web_job(self._webclient.get_storage_info)
                if isinstance(storage_info, dict):
                    self._client.set_storage_info(
                        manufacturer=storage_info.get("manufacturer"),
                        model=storage_info.get("model"),
                        serial=storage_info.get("serial"),
                    )
                    self.data["storage_temperature"] = storage_info.get("cell_temperature")

        if self.web_api_configured:
            await self.refresh_web_data()

        if setup_coordinator:
            # Initialize the coordinator. The config-entry first refresh API
            # is only valid when a config entry is available.
            self.coordinator = FroniusCoordinator(self._hass, self, config_entry=config_entry)
            if config_entry is not None:
                await self.coordinator.async_config_entry_first_refresh()
            else:
                await self.coordinator.async_refresh()

        return

    async def validate_web_api(self) -> bool:
        if not self._webclient:
            return False
        return await self._hass.async_add_executor_job(self._webclient.login)

    async def _async_refresh_optional_data(self) -> None:
        self.data["load"] = None
        await self._async_optional_poll(
            "inverter status",
            self._client.read_inverter_status_data,
            stale_keys=INVERTER_STATUS_DATA_KEYS,
        )
        await self._async_optional_poll(
            "inverter settings",
            self._client.read_inverter_model_settings_data,
            stale_keys=INVERTER_SETTINGS_DATA_KEYS,
        )
        await self._async_optional_poll(
            "inverter controls",
            self._client.read_inverter_controls_data,
            stale_keys=INVERTER_CONTROL_DATA_KEYS,
        )

        if self._client.meter_configured:
            if self._client.primary_meter_unit_id not in self._client._meter_unit_ids:
                self.data["load"] = None
                self.data["grid_status"] = None

            for meter_address in self._client._meter_unit_ids:
                await self._async_optional_poll(
                    f"meter {meter_address}",
                    self._client.read_meter_data,
                    unit_id=meter_address,
                    is_primary=meter_address == self._client.primary_meter_unit_id,
                    stale_keys=self._meter_data_keys(meter_address),
                )

        if self._client.mppt_configured:
            await self._async_optional_poll(
                "mppt",
                self._client.read_mppt_data,
                stale_keys=self._mppt_data_keys(),
            )

        await self._async_optional_poll(
            "ac limit",
            self._client.read_ac_limit_data,
            stale_keys=AC_LIMIT_DATA_KEYS,
        )

        if self._client.storage_configured:
            await self._async_optional_poll(
                "storage",
                self._client.read_inverter_storage_data,
                stale_keys=STORAGE_DATA_KEYS,
            )

        self._apply_modbus_load_data()

        if self.web_api_configured:
            try:
                await self.refresh_web_data()
            except Exception as err:
                _LOGGER.warning("Fronius web API refresh failed: %s", err)

    async def _async_optional_poll(self, label: str, func, *args, stale_keys=(), **kwargs) -> bool:
        try:
            result = await func(*args, **kwargs)
        except Exception as err:
            _LOGGER.warning("Optional Fronius %s refresh failed: %s", label, err)
            self._clear_data_keys(stale_keys)
            return False

        if result is False:
            _LOGGER.debug("Optional Fronius %s refresh returned no data", label)
            self._clear_data_keys(stale_keys)
            return False
        return True

    def _clear_data_keys(self, keys) -> None:
        for key in keys:
            self.data[key] = None

    def _meter_data_keys(self, unit_id: int) -> tuple[str, ...]:
        prefix = self._meter_prefix(unit_id)
        keys = [f"{prefix}{suffix}" for suffix in METER_DATA_KEY_SUFFIXES]
        if int(unit_id) == self._client.primary_meter_unit_id:
            keys.append("grid_status")
        return tuple(keys)

    def _mppt_data_keys(self) -> tuple[str, ...]:
        keys = [
            "pv_power",
            "mppt_visible_module_ids",
            "storage_charge_module",
            "storage_charge_current",
            "storage_charge_voltage",
            "storage_charge_power",
            "storage_charge_lfte",
            "storage_discharge_module",
            "storage_discharge_current",
            "storage_discharge_voltage",
            "storage_discharge_power",
            "storage_discharge_lfte",
        ]
        for module_id in range(1, int(self._client.mppt_module_count) + 1):
            module_idx = module_id - 1
            keys.extend(
                (
                    f"module{module_id}_label",
                    f"module{module_id}_power",
                    f"module{module_id}_lfte",
                    f"module{module_id}_tms",
                    f"mppt_module_{module_idx}_label",
                    f"mppt_module_{module_idx}_dc_current",
                    f"mppt_module_{module_idx}_dc_voltage",
                    f"mppt_module_{module_idx}_dc_power",
                    f"mppt_module_{module_idx}_lifetime_energy",
                    f"mppt_module_{module_idx}_timestamp",
                )
            )
        return tuple(keys)

    def _clear_web_api_data(self) -> None:
        for key in WEB_API_DATA_KEYS:
            self.data[key] = None

    def _battery_write_transition_active(self) -> bool:
        return time.monotonic() < self._battery_write_transition_until

    def _clear_battery_write_transition(self) -> None:
        self._battery_write_transition_until = 0.0
        self._battery_write_transition_warned = False

    def _schedule_delayed_web_refresh(self) -> None:
        if self._delayed_web_refresh_task and not self._delayed_web_refresh_task.done():
            self._delayed_web_refresh_task.cancel()

        async def delayed_refresh() -> None:
            try:
                await asyncio.sleep(BATTERY_WRITE_WEB_REFRESH_DELAY_SECONDS)
                if self._webclient:
                    await self.refresh_web_data()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Delayed Fronius web API refresh failed: %s", err)

        self._delayed_web_refresh_task = self._hass.loop.create_task(delayed_refresh())

    def _start_battery_write_transition(self, source: str) -> None:
        self._battery_write_transition_until = (
            time.monotonic() + BATTERY_WRITE_MODBUS_RECOVERY_SECONDS
        )
        self._battery_write_transition_warned = False
        self._client.close()
        self._schedule_delayed_web_refresh()
        _LOGGER.debug(
            "Started Modbus recovery window after %s write for %s",
            source,
            self._host,
        )

    def _handle_core_modbus_success(self) -> None:
        if self._battery_write_transition_active():
            _LOGGER.debug("Modbus recovered after battery API write on %s", self._host)
        self._clear_battery_write_transition()

    def _handle_core_modbus_failure(self, err: Exception) -> bool:
        if not self._battery_write_transition_active():
            return False

        if not self._battery_write_transition_warned:
            _LOGGER.warning(
                "Suppressing temporary Modbus outage after battery API write on %s: %s",
                self._host,
                err,
            )
            self._battery_write_transition_warned = True
        else:
            _LOGGER.debug(
                "Modbus still recovering after battery API write on %s: %s",
                self._host,
                err,
            )
        return True

    def _set_effective_api_battery_mode(
        self,
        raw_mode: int | None,
        raw_soc_mode: str | None,
    ) -> None:
        effective_mode = self._derive_api_battery_mode(raw_mode, raw_soc_mode)
        self.data['api_battery_mode_raw'] = raw_mode
        self.data['api_battery_mode_effective_raw'] = effective_mode
        self.data['api_battery_mode_consistent'] = effective_mode is not None
        self.data['api_battery_mode'] = (
            API_BATTERY_MODE.get(effective_mode) if effective_mode is not None else None
        )
        self.data['api_soc_mode_raw'] = raw_soc_mode
        self.data['api_soc_mode'] = API_SOC_MODE.get(raw_soc_mode, raw_soc_mode)

    async def _async_handle_web_api_auth_failure(self, err: Exception) -> None:
        if not self._webclient:
            return

        _LOGGER.warning("Disabling Fronius web API for %s after auth failure: %s", self._host, err)
        self._webclient = None
        self._clear_web_api_data()
        await async_get_token_store(self._hass).async_delete_token(
            self._host,
            self._api_username or API_USERNAME,
        )
        await self._async_sync_solar_api_warning()

        if self._config_entry is not None:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{self._config_entry.entry_id}",
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="legacy_modbus_only_entry_reconfigure",
                translation_placeholders={"entry_title": self._config_entry.title or self._name},
                data={"entry_id": self._config_entry.entry_id},
            )

    async def _async_web_job(
        self,
        func,
        *args,
        raise_on_auth_failure: bool = False,
    ):
        if not self._webclient:
            if raise_on_auth_failure:
                raise RuntimeError("Fronius Web API is not configured")
            return None

        try:
            return await self._hass.async_add_executor_job(func, *args)
        except FroniusWebAuthError as err:
            await self._async_handle_web_api_auth_failure(err)
            if raise_on_auth_failure:
                raise RuntimeError(
                    "Fronius Web API authentication failed. Reconfigure the integration."
                ) from err
            return None

    def _apply_web_battery_config(self, battery_config: dict[str, Any]) -> None:
        raw_mode = self._as_int(battery_config.get('HYB_EM_MODE'))
        raw_power = self._as_int(battery_config.get('HYB_EM_POWER'))

        raw_soc_mode = battery_config.get('BAT_M0_SOC_MODE')
        if isinstance(raw_soc_mode, str):
            raw_soc_mode = raw_soc_mode.lower()
        else:
            raw_soc_mode = None

        effective_mode = self._derive_api_battery_mode(raw_mode, raw_soc_mode)
        self._set_effective_api_battery_mode(raw_mode, raw_soc_mode)
        self.data['api_battery_power'] = -raw_power if raw_power is not None else None
        api_soc_min = self._as_int(battery_config.get('BAT_M0_SOC_MIN'))
        self.data['api_soc_min'] = api_soc_min
        self.data['soc_maximum'] = self._as_int(battery_config.get('BAT_M0_SOC_MAX'))
        self.data['api_backup_reserved'] = self._as_int(battery_config.get('HYB_BACKUP_RESERVED'))
        if effective_mode == 1 and api_soc_min is not None:
            self.data['soc_minimum'] = api_soc_min
        self.data['api_charge_from_ac'] = self._enabled_bool(battery_config.get('HYB_BM_CHARGEFROMAC'))
        self.data['api_charge_from_grid'] = self._enabled_bool(battery_config.get('HYB_EVU_CHARGEFROMGRID'))

    def _apply_web_modbus_config(self, modbus_config: dict[str, Any]) -> None:
        slave = modbus_config.get('slave') or {}
        ctr = slave.get('ctr') or {}
        restriction = ctr.get('restriction') or {}
        mode = slave.get('mode')

        self.data['api_modbus_mode'] = str(mode).upper() if mode is not None else None
        self.data['api_modbus_control'] = self._enabled_state(ctr.get('on'))
        self.data['api_modbus_sunspec_mode'] = slave.get('sunspecMode')
        self.data['api_modbus_restriction'] = self._enabled_state(restriction.get('on'))
        self.data['api_modbus_restriction_ip'] = restriction.get('ip')

    def _apply_modbus_load_data(self) -> None:
        self.data["load"] = None
        if not self._client.meter_configured:
            self._reset_bad_load_tracking()
            return

        primary_unit_id = self._client.primary_meter_unit_id
        meter_prefix = self._meter_prefix(primary_unit_id)
        meter_location = self._as_int(self.data.get(f"{meter_prefix}location"))
        meter_power = self.data.get(f"{meter_prefix}power")
        inverter_power = self.data.get("acpower")
        inverter_sample_ts, meter_sample_ts = self._client.get_load_sample_timestamps(primary_unit_id)
        if meter_sample_ts is None or meter_power is None or not self._client.is_numeric(meter_power):
            self._reset_bad_load_tracking()
            return
        meter_power_f = float(meter_power)

        if meter_location == 1 or (
            meter_location is not None and 256 <= meter_location <= 511
        ):
            self._set_load(-meter_power_f, cache=meter_power_f <= 0)
            return

        if meter_location != 0:
            self._reset_bad_load_tracking()
            return

        if (
            inverter_sample_ts is None
            or inverter_power is None
            or not self._client.is_numeric(inverter_power)
            or abs(inverter_sample_ts - meter_sample_ts) > LOAD_MAX_SAMPLE_SKEW_SECONDS
        ):
            # Do not mix inverter and meter values from different poll moments.
            self._reset_bad_load_tracking()
            return
        inverter_power_f = float(inverter_power)

        candidate_load = meter_power_f + inverter_power_f
        if self._is_inverter_power_glitch(
            candidate_load=candidate_load,
            meter_power=meter_power_f,
            inverter_power=inverter_power_f,
        ):
            self._apply_glitch_load_fallback()
            return

        storage_charge_power = self.data.get("storage_charge_power")
        if (
            candidate_load < 0
            and self.storage_configured
            and self._client.is_numeric(storage_charge_power)
            and float(storage_charge_power) >= LOAD_STORAGE_CHARGE_MIN_W
        ):
            # Battery charging can make the simple formula lie; prefer unavailable over bogus 0 W.
            self._reset_bad_load_tracking()
            return

        self._set_load(max(candidate_load, 0.0), cache=True, inverter_power=inverter_power_f)

    async def refresh_web_data(self) -> None:
        if not self._webclient:
            return

        inverter_info = await self._async_web_job(self._webclient.get_inverter_info)
        if isinstance(inverter_info, dict):
            self.data["inverter_temperature"] = inverter_info.get("temperature")
        else:
            self.data["inverter_temperature"] = None

        modbus_config = await self._async_web_job(self._webclient.get_modbus_config)
        if isinstance(modbus_config, dict):
            self._apply_web_modbus_config(modbus_config)

        solar_api_config = await self._async_web_job(self._webclient.get_solar_api_config)
        if isinstance(solar_api_config, dict):
            enabled = solar_api_config.get("SolarAPIv1Enabled")
            self.data["api_solar_api_enabled"] = (
                self._enabled_bool(enabled) if enabled is not None else None
            )
        else:
            self.data["api_solar_api_enabled"] = None

        if self.storage_configured:
            storage_info = await self._async_web_job(self._webclient.get_storage_info)
            if isinstance(storage_info, dict):
                self.data["storage_temperature"] = storage_info.get("cell_temperature")
            else:
                self.data["storage_temperature"] = None

            battery_config = await self._async_web_job(self._webclient.get_battery_config)
            if isinstance(battery_config, dict):
                self._apply_web_battery_config(battery_config)

        if self.tech_configured:
            export_limit_config = await self._async_web_job(self._webclient.get_export_limit_config)
        else:
            export_limit_config = None
        export_limit_summary = _export_limit_summary(export_limit_config)
        if export_limit_summary != self._last_export_limit_summary:
            _LOGGER.debug("Export limit config from web API: %s", export_limit_summary)
            self._last_export_limit_summary = export_limit_summary
        self.data["export_soft_limit"] = None
        if isinstance(export_limit_config, dict) and export_limit_config:
            soft = (
                export_limit_config.get("exportLimits", {})
                .get("activePower", {})
                .get("softLimit", {})
            )
            if isinstance(soft, dict):
                if soft.get("enabled"):
                    self.data["export_soft_limit"] = soft.get("powerLimit")

        await self._async_sync_solar_api_warning()

    @_serialized_write
    async def set_solar_api_enabled(self, enabled: bool) -> None:
        if not self._webclient:
            return

        await self._async_web_job(
            self._webclient.set_solar_api_enabled,
            enabled,
            raise_on_auth_failure=True,
        )
        self.data["api_solar_api_enabled"] = bool(enabled)
        await self._async_sync_solar_api_warning()

    @_serialized_write
    async def reset_modbus_control(self) -> None:
        if not self._webclient:
            return

        await self._async_web_job(
            self._webclient.reset_modbus_control,
            raise_on_auth_failure=True,
        )

    def _get_next_soc_limits(
        self,
        *,
        soc_min: int | None = None,
        soc_max: int | None = None,
    ) -> tuple[int, int]:
        next_soc_min = self._as_int(self.data.get('soc_minimum')) if soc_min is None else int(soc_min)
        next_soc_max = self._as_int(self.data.get('soc_maximum')) if soc_max is None else int(soc_max)

        next_soc_min = 5 if next_soc_min is None else next_soc_min
        next_soc_max = 99 if next_soc_max is None else next_soc_max

        if next_soc_min < 5 or next_soc_min > 100:
            raise ValueError('SoC Minimum must be between 5 and 100')
        if next_soc_max < 0 or next_soc_max > 100:
            raise ValueError('SoC Maximum must be between 0 and 100')
        if next_soc_min > next_soc_max:
            raise ValueError('SoC Minimum must not exceed SoC Maximum')

        return next_soc_min, next_soc_max

    def _get_api_soc_values(
        self,
        *,
        soc_min: int | None = None,
        soc_max: int | None = None,
    ) -> tuple[int, int, int]:
        next_soc_min, next_soc_max = self._get_next_soc_limits(
            soc_min=soc_min,
            soc_max=soc_max,
        )
        next_backup_reserved = self._as_int(self.data.get('api_backup_reserved'))
        next_backup_reserved = 5 if next_backup_reserved is None else next_backup_reserved
        if next_backup_reserved < 5 or next_backup_reserved > 100:
            raise ValueError('Battery backup reserve must be between 5 and 100')

        return next_soc_min, next_soc_max, next_backup_reserved

    def _derive_api_battery_mode(
        self,
        raw_mode: int | None,
        raw_soc_mode: str | None,
    ) -> int | None:
        if raw_mode == 1 and raw_soc_mode == 'manual':
            return 1
        if raw_mode == 0 and raw_soc_mode == 'auto':
            return 0
        return None

    def _api_battery_mode_is_manual(self) -> bool:
        return self._as_int(self.data.get('api_battery_mode_effective_raw')) == 1

    def _require_api_battery_mode_manual(self, control_name: str) -> None:
        if not self._api_battery_mode_is_manual():
            raise ValueError(f'{control_name} can only be changed when Battery API mode is Manual')

    async def _set_api_soc_manual(
        self,
        soc_min: int | None = None,
        soc_max: int | None = None,
        control_name: str = 'SoC Maximum',
    ) -> tuple[int, int, int] | None:
        if not self._webclient:
            return None
        self._require_api_battery_mode_manual(control_name)

        next_soc_min, next_soc_max, next_backup_reserved = self._get_api_soc_values(
            soc_min=soc_min,
            soc_max=soc_max,
        )
        await self._async_web_job(
            self._webclient.set_battery_soc_config,
            next_soc_min,
            next_soc_max,
            next_backup_reserved,
            raise_on_auth_failure=True,
        )
        self._set_effective_api_battery_mode(1, 'manual')
        self.data['soc_minimum'] = next_soc_min
        self.data['api_soc_min'] = next_soc_min
        self.data['soc_maximum'] = next_soc_max
        self.data['api_backup_reserved'] = next_backup_reserved
        self._start_battery_write_transition(control_name)
        return next_soc_min, next_soc_max, next_backup_reserved

    def check_pymodbus_version(self):
        try:
            current_version = version('pymodbus')
            if current_version is None:
                _LOGGER.warning(f"pymodbus not found")
                return

            current = pkg_version.parse(current_version)
            required = pkg_version.parse(self.PYMODBUS_VERSION)

            if current < required:
                raise Exception(f"pymodbus {current_version} found, please update to {self.PYMODBUS_VERSION} or higher")
            elif current > required:
                _LOGGER.info(f"newer pymodbus {current_version} found")
            _LOGGER.debug(f"pymodbus {current_version}")
        except Exception as e:
            _LOGGER.error(f"Error checking pymodbus version: {e}")
            raise

    def _as_int(self, value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _require_whole_number(self, value: Any, field_name: str) -> int:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as err:
            raise ValueError(f"{field_name} must be a whole number") from err

        if not numeric_value.is_integer():
            raise ValueError(f"{field_name} must be a whole number")

        return int(numeric_value)

    def _enabled_state(self, value: Any) -> str:
        if isinstance(value, str):
            normalized = value.strip().lower()
            is_enabled = normalized in ['1', 'true', 'on', 'yes', 'enabled']
        else:
            is_enabled = bool(value)
        return 'Enabled' if is_enabled else 'Disabled'

    def _enabled_bool(self, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in ['1', 'true', 'on', 'yes', 'enabled']
        return bool(value)

    @property 
    def device_info_storage(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f'{self.instance_key}_battery_storage')},
            "name": f'{self._client.data.get('s_model')}',
            "manufacturer": self._client.data.get('s_manufacturer'),
            "model": self._client.data.get('s_model'),
            "serial_number": self._client.data.get('s_serial'),
        }

    @property 
    def device_info_inverter(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f'{self.instance_key}_inverter')},
            "name": f'Fronius {self._client.data.get('i_model')}',
            "manufacturer": self._client.data.get('i_manufacturer'),
            "model": self._client.data.get('i_model'),
            "serial_number": self._client.data.get('i_serial'),
            "sw_version": self._client.data.get('i_sw_version'),
        }
    
    def get_device_info_meter(self, unit_id: int) -> dict:
        prefix = self._meter_prefix(unit_id)
        try:
            meter_position = self._client._meter_unit_ids.index(unit_id) + 1
        except ValueError:
            meter_position = 1
        return {
            "identifiers": {(DOMAIN, f'{self.instance_key}_meter_{unit_id}')},
            "name": f'Fronius {self._client.data.get(f"{prefix}model")} Meter {meter_position}',
            "manufacturer": self._client.data.get(f"{prefix}manufacturer"),
            "model": self._client.data.get(f"{prefix}model"),
            "serial_number": self._client.data.get(f"{prefix}serial"),
            "sw_version": self._client.data.get(f"{prefix}sw_version"),
        }

    @property
    def hub_id(self) -> str:
        """ID for hub."""
        return self._id

    @property
    def instance_key(self) -> str:
        """Stable per-entry key for entity and device registry identity."""
        if self._config_entry is not None:
            return self._normalize_instance_key(self._config_entry.entry_id)
        return self._fallback_instance_key

    @property
    def entity_prefix(self) -> str:
        """Entity prefix for hub."""
        return f"{ENTITY_PREFIX}_{self.instance_key}"



    def close(self):
        """Disconnect client."""
        if self._delayed_web_refresh_task and not self._delayed_web_refresh_task.done():
            self._delayed_web_refresh_task.cancel()
        self._client.close()

    @property
    def data(self):
        return self._client.data

    @property
    def web_api_configured(self) -> bool:
        return self._webclient is not None

    @property
    def tech_configured(self) -> bool:
        return self.web_api_configured and self._api_username == TECHNICIAN_USERNAME

    @property
    def meter_configured(self):
        return self._client.meter_configured

    @property
    def storage_configured(self):
        return self._client.storage_configured

    @property
    def max_discharge_rate_w(self):
        return self._client.max_discharge_rate_w

    @property
    def max_charge_rate_w(self):
        return self._client.max_charge_rate_w

    @property
    def storage_extended_control_mode(self):
        return self._client.storage_extended_control_mode

    @_serialized_write
    async def set_mode(self, mode):
        if mode == 0:
            await self._client.set_auto_mode()
        elif mode == 1:
            await self._client.set_charge_mode()
        elif mode == 2:
            await self._client.set_discharge_mode()
        elif mode == 3:
            await self._client.set_charge_discharge_mode()
        elif mode == 4:
            await self._client.set_grid_charge_mode()
            if self._webclient:
                try:
                    await self._set_api_charge_sources(
                        charge_from_grid=True,
                        charge_from_ac=True,
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Failed enabling Web API charge-source toggles after Modbus Charge from Grid: %s",
                        err,
                    )
        elif mode == 5:
            await self._client.set_grid_discharge_mode()
        elif mode == 6:
            await self._client.set_block_discharge_mode()
        elif mode == 7:
            await self._client.set_block_charge_mode()

    @_serialized_write
    async def set_soc_minimum(self, value):
        soc_minimum = self._require_whole_number(value, 'SoC Minimum')
        if soc_minimum < 5 or soc_minimum > 100:
            raise ValueError('SoC Minimum must be between 5 and 100')
        if self._webclient and self._api_battery_mode_is_manual():
            self._get_next_soc_limits(soc_min=soc_minimum)
        await self._client.set_minimum_reserve(soc_minimum)
        self.data['soc_minimum'] = soc_minimum
        if self._webclient and self._api_battery_mode_is_manual():
            await self._set_api_soc_manual(soc_min=soc_minimum, control_name='SoC Minimum')

    @_serialized_write
    async def set_charge_limit(self, value):
        await self._client.set_charge_limit(value)

    @_serialized_write
    async def set_discharge_limit(self, value):
        await self._client.set_discharge_limit(value)

    @_serialized_write
    async def set_grid_charge_power(self, value):
        await self._client.set_grid_charge_power(value)
           
    @_serialized_write
    async def set_grid_discharge_power(self, value):
        await self._client.set_grid_discharge_power(value)

    @_serialized_write
    async def set_api_battery_mode(self, mode: int):
        if not self._webclient:
            return

        current_effective_mode = self._as_int(self.data.get('api_battery_mode_effective_raw'))
        display_power = self._as_int(self.data.get('api_battery_power'))
        if mode == 1 and display_power is None:
            display_power = 0
        power = -display_power if mode == 1 and display_power is not None else None
        soc_min = None
        if mode == 1 and current_effective_mode != 1:
            soc_min = self._as_int(self.data.get('soc_minimum'))
        await self._async_web_job(
            self._webclient.set_battery_config,
            mode,
            power,
            soc_min,
            raise_on_auth_failure=True,
        )
        self._set_effective_api_battery_mode(mode, 'manual' if mode == 1 else 'auto')
        if mode == 1:
            self.data['api_battery_power'] = display_power
            if soc_min is not None:
                self.data['api_soc_min'] = soc_min
        else:
            self.data['api_soc_min'] = 5
            self.data['soc_maximum'] = 100
        self._start_battery_write_transition('Battery API mode')

    @_serialized_write
    async def set_api_battery_power(self, value: float):
        if not self._webclient:
            return
        self._require_api_battery_mode_manual('Target feed in')

        power = -int(round(value))
        await self._async_web_job(
            self._webclient.set_battery_config,
            1,
            power,
            raise_on_auth_failure=True,
        )
        self.data['api_battery_power'] = int(round(value))
        self._set_effective_api_battery_mode(1, 'manual')
        self._start_battery_write_transition('Target feed in')

    @_serialized_write
    async def set_api_soc_values(
        self,
        soc_max: int | None = None,
    ):
        if not self._webclient:
            return

        await self._set_api_soc_manual(
            soc_max=soc_max,
            control_name='SoC Maximum',
        )

    async def _set_api_charge_sources(
        self,
        *,
        charge_from_grid: bool | None = None,
        charge_from_ac: bool | None = None,
    ) -> None:
        if not self._webclient:
            return

        if charge_from_ac is False:
            next_charge_from_grid = False
            next_charge_from_ac = False
        else:
            next_charge_from_grid = (
                self._enabled_bool(self.data.get('api_charge_from_grid'))
                if charge_from_grid is None
                else bool(charge_from_grid)
            )
            next_charge_from_ac = (
                self._enabled_bool(self.data.get('api_charge_from_ac'))
                if charge_from_ac is None
                else bool(charge_from_ac)
            )
            if next_charge_from_grid and charge_from_ac is None:
                next_charge_from_ac = True

        await self._async_web_job(
            self._webclient.set_battery_charge_sources,
            next_charge_from_grid,
            next_charge_from_ac,
            raise_on_auth_failure=True,
        )
        self.data['api_charge_from_grid'] = next_charge_from_grid
        self.data['api_charge_from_ac'] = next_charge_from_ac
        self._start_battery_write_transition('battery charge source')

    @_serialized_write
    async def set_api_charge_sources(
        self,
        *,
        charge_from_grid: bool | None = None,
        charge_from_ac: bool | None = None,
    ) -> None:
        await self._set_api_charge_sources(
            charge_from_grid=charge_from_grid,
            charge_from_ac=charge_from_ac,
        )

    @_serialized_write
    async def set_ac_limit_rate(self, value):
        await self._client.set_ac_limit_rate(value)

    @_serialized_write
    async def set_ac_limit_enable(self, value):
        await self._client.set_ac_limit_enable(value)

    @_serialized_write
    async def set_power_factor(self, value):
        await self._client.set_power_factor(value)

    @_serialized_write
    async def set_power_factor_enable(self, value):
        await self._client.set_power_factor_enable(value)

    @_serialized_write
    async def set_conn_status(self, enable):
        await self._client.set_conn_status(enable)

    @_serialized_write
    async def set_export_soft_limit(self, value: float) -> None:
        if not self.tech_configured:
            raise RuntimeError(
                "Technician access is not selected — select it via Configure"
            )
        await self._async_web_job(
            self._webclient.set_export_soft_limit,
            int(round(value)),
            raise_on_auth_failure=True,
        )
        self.data["export_soft_limit"] = int(round(value))
