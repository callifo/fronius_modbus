"""The web-API half of the integration: readings and battery controls Modbus does not offer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
import functools
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    API_BATTERY_MODE,
    API_SOC_MODE,
    API_USERNAME,
    DOMAIN,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
    SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX,
)
from .froniuswebclient import FroniusWebAuthError, FroniusWebClient
from .token_store import async_get_token_store

_LOGGER = logging.getLogger(__name__)
WEB_API_NOT_CONFIGURED = "Fronius Web API is not configured"
TECHNICIAN_NOT_CONFIGURED = "Technician credentials not configured - enter the technician password via Configure"


def _serialised(method):
    """Run one web write at a time: every setter reads state, composes a payload and writes it back."""

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with self._write_lock:
            return await method(self, *args, **kwargs)

    return wrapper


# The inverter drops Modbus for a while after a battery configuration write over the web API.
BATTERY_WRITE_MODBUS_RECOVERY_SECONDS = 30.0
BATTERY_WRITE_WEB_REFRESH_DELAY_SECONDS = 10.0
SOLAR_API_MINIMUM_VERSION = (1, 40, 7, 1)
SOLAR_API_MINIMUM_VERSION_TEXT = "1.40.7-1"
SOLAR_API_WARNING_TRANSLATION_KEY = "solar_api_low_firmware"
_FIRMWARE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(\d+))?$")
BATTERY_MODE_AUTO, BATTERY_MODE_MANUAL = 0, 1
SOC_MODE_AUTO, SOC_MODE_MANUAL = "auto", "manual"
SOC_LOWEST, SOC_HIGHEST, SOC_MAX_DEFAULT = 5, 100, 99


@dataclass
class WebData:
    """The web API's contribution to the entities' state, refreshed on its own schedule."""

    inverter_temperature: float | None = None
    modbus_mode: str | None = None
    modbus_control: str | None = None
    sunspec_mode: str | None = None
    modbus_restriction: str | None = None
    modbus_restriction_ip: str | None = None
    solar_api_enabled: bool | None = None
    storage_temperature: float | None = None
    battery_mode_raw: int | None = None
    battery_mode_effective: int | None = None
    battery_mode: str | None = None
    soc_mode_raw: str | None = None
    soc_mode: str | None = None
    battery_power_w: int | None = None
    soc_min: int | None = None
    soc_max: int | None = None
    backup_reserved: int | None = None
    charge_from_ac: bool | None = None
    charge_from_grid: bool | None = None
    export_soft_limit_w: int | None = None
    storage_manufacturer: str | None = None
    storage_model: str | None = None
    storage_serial: str | None = None


def _export_limit_summary(config: dict[str, Any] | None) -> dict[str, Any]:
    """Distill the export-limit payload for change-logging."""
    if not isinstance(config, dict) or not config:
        return {"available": False}

    export_limits = config.get("exportLimits", {})
    active_power = (
        export_limits.get("activePower", {}) if isinstance(export_limits, dict) else {}
    )
    soft_limit = (
        active_power.get("softLimit", {}) if isinstance(active_power, dict) else {}
    )
    hard_limit = (
        active_power.get("hardLimit", {}) if isinstance(active_power, dict) else {}
    )

    return {
        "available": True,
        "active_power_activated": active_power.get("activated")
        if isinstance(active_power, dict)
        else None,
        "soft_limit_enabled": soft_limit.get("enabled")
        if isinstance(soft_limit, dict)
        else None,
        "soft_limit_w": soft_limit.get("powerLimit")
        if isinstance(soft_limit, dict)
        else None,
        "hard_limit_enabled": hard_limit.get("enabled")
        if isinstance(hard_limit, dict)
        else None,
        "hard_limit_w": hard_limit.get("powerLimit")
        if isinstance(hard_limit, dict)
        else None,
        "fail_safe_enabled": export_limits.get("failSafeModeEnabled")
        if isinstance(export_limits, dict)
        else None,
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except TypeError, ValueError:
        return None


def _enabled_state(value: Any) -> str:
    return "enabled" if _enabled_bool(value) else "disabled"


def _enabled_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes", "enabled")
    return bool(value)


def _parse_firmware_version(version_text: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(version_text, str):
        return None

    match = _FIRMWARE_RE.fullmatch(version_text.strip())
    if match is None:
        return None

    major, minor, patch, build = match.groups(default="0")
    return (int(major), int(minor), int(patch), int(build))


def _derive_api_battery_mode(raw_mode: int | None) -> int | None:
    # The inverter leaves BAT_M0_SOC_MODE at "manual" after any SoC write, so
    # that field must not veto HYB_EM_MODE as the source of truth.
    if raw_mode == BATTERY_MODE_MANUAL:
        return BATTERY_MODE_MANUAL
    if raw_mode == BATTERY_MODE_AUTO:
        return BATTERY_MODE_AUTO
    return None


class FroniusWebControl:
    """Solar-API state and battery/export controls, alongside the Modbus poll."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry | None,
        *,
        host: str,
        client: FroniusWebClient | None,
        technician_client: FroniusWebClient | None,
        storage_present: bool,
        inverter_firmware: Callable[[], str | None],
        on_battery_write: Callable[[], None],
        modbus_soc_minimum: Callable[[], int | None] = lambda: None,
    ) -> None:
        """Bind to the web clients; on_battery_write opens the Modbus recovery window."""
        self._hass = hass
        self._entry = entry
        self._host = host
        self._client = client
        self._technician_client = technician_client
        self._storage_present = storage_present
        self._inverter_firmware = inverter_firmware
        self._on_battery_write = on_battery_write
        self._modbus_soc_minimum = modbus_soc_minimum
        # One write at a time (audit F10): concurrent read-modify-write of the
        # SoC tuple or the charge-source pair lost one of the two changes.
        self._write_lock = asyncio.Lock()
        self.data = WebData()
        self._coordinator: Any = None
        self._delayed_refresh_task: asyncio.Task | None = None

    def attach_coordinator(self, coordinator: Any) -> None:
        """Bind the web coordinator so the delayed post-write refresh can push new data."""
        self._coordinator = coordinator

    @property
    def configured(self) -> bool:
        """Whether the customer web API client is available."""
        return self._client is not None

    @property
    def technician_configured(self) -> bool:
        """Whether the technician web API client is available."""
        return self._technician_client is not None

    @property
    def battery_mode_is_manual(self) -> bool:
        """Whether the battery is confirmed to be in Manual mode."""
        return self.data.battery_mode_effective == BATTERY_MODE_MANUAL

    def shutdown(self) -> None:
        """Cancel the delayed post-write refresh; called from entry.async_on_unload."""
        if self._delayed_refresh_task and not self._delayed_refresh_task.done():
            self._delayed_refresh_task.cancel()

    # -- solar API firmware warning -------------------------------------------------

    def _solar_api_warning_issue_id(self) -> str | None:
        if self._entry is None:
            return None
        return f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}{self._entry.entry_id}"

    def _solar_api_warning_needed(self, firmware: str | None) -> bool:
        if not self.configured:
            return False

        firmware_version = _parse_firmware_version(firmware)
        if firmware_version is None:
            return False

        if self.data.solar_api_enabled is not True:
            return False

        return firmware_version < SOLAR_API_MINIMUM_VERSION

    def _async_sync_solar_api_warning(self) -> None:
        issue_id = self._solar_api_warning_issue_id()
        if issue_id is None:
            return

        # Read on every sync, not once at construction: a firmware update has
        # to clear the issue on the next poll instead of at the next restart.
        firmware = self._inverter_firmware()
        if not self._solar_api_warning_needed(firmware):
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return

        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=SOLAR_API_WARNING_TRANSLATION_KEY,
            translation_placeholders={
                "entry_title": self._entry.title
                if self._entry is not None
                else self._host,
                "current_version": str(firmware),
                "minimum_version": SOLAR_API_MINIMUM_VERSION_TEXT,
            },
            data={
                "entry_id": self._entry.entry_id if self._entry is not None else None,
                "current_version": str(firmware),
                "minimum_version": SOLAR_API_MINIMUM_VERSION_TEXT,
            },
        )

    # -- job runners -----------------------------------------------------------------

    async def _async_web_job(self, func, *args, raise_on_auth_failure: bool = False):
        if not self._client:
            if raise_on_auth_failure:
                raise RuntimeError(WEB_API_NOT_CONFIGURED)
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

    async def _async_client_job(self, method_name: str, *args):
        """Run one refresh step, re-reading the client: an auth failure clears it mid-refresh."""
        client = self._client
        if client is None:
            return None
        return await self._async_web_job(getattr(client, method_name), *args)

    async def _async_tech_web_job(
        self, func, *args, raise_on_auth_failure: bool = False
    ):
        """Run a technician-client job; on auth failure clear only the technician client."""
        if not self._technician_client:
            if raise_on_auth_failure:
                raise RuntimeError(TECHNICIAN_NOT_CONFIGURED)
            return None
        try:
            return await self._hass.async_add_executor_job(func, *args)
        except FroniusWebAuthError as err:
            _LOGGER.warning(
                "Disabling Fronius technician web API for %s after auth failure: %s",
                self._host,
                err,
            )
            self._technician_client = None
            self.data.export_soft_limit_w = None
            if raise_on_auth_failure:
                raise RuntimeError(
                    "Fronius technician authentication failed. Reconfigure the integration."
                ) from err
            return None

    async def _async_handle_web_api_auth_failure(self, err: Exception) -> None:
        if not self._client:
            return

        _LOGGER.warning(
            "Disabling Fronius web API for %s after auth failure: %s", self._host, err
        )
        self._client = None
        self.data = WebData()
        await async_get_token_store(self._hass).async_delete_token(
            self._host, API_USERNAME
        )
        self._async_sync_solar_api_warning()

        if self._entry is not None:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{self._entry.entry_id}",
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="legacy_modbus_only_entry_reconfigure",
                translation_placeholders={
                    "entry_title": self._entry.title or self._host
                },
                data={"entry_id": self._entry.entry_id},
            )

    # -- refresh -----------------------------------------------------------------

    def _apply_web_battery_config(self, battery_config: dict[str, Any]) -> None:
        raw_mode = _as_int(battery_config.get("HYB_EM_MODE"))
        raw_power = _as_int(battery_config.get("HYB_EM_POWER"))

        raw_soc_mode = battery_config.get("BAT_M0_SOC_MODE")
        raw_soc_mode = raw_soc_mode.lower() if isinstance(raw_soc_mode, str) else None

        self._set_effective_battery_mode(raw_mode, raw_soc_mode)
        self.data.battery_power_w = -raw_power if raw_power is not None else None
        self.data.soc_min = _as_int(battery_config.get("BAT_M0_SOC_MIN"))
        self.data.soc_max = _as_int(battery_config.get("BAT_M0_SOC_MAX"))
        self.data.backup_reserved = _as_int(battery_config.get("HYB_BACKUP_RESERVED"))
        self.data.charge_from_ac = _enabled_bool(
            battery_config.get("HYB_BM_CHARGEFROMAC")
        )
        self.data.charge_from_grid = _enabled_bool(
            battery_config.get("HYB_EVU_CHARGEFROMGRID")
        )

    def _apply_web_modbus_config(self, modbus_config: dict[str, Any]) -> None:
        slave = modbus_config.get("slave") or {}
        ctr = slave.get("ctr") or {}
        restriction = ctr.get("restriction") or {}
        mode = slave.get("mode")

        self.data.modbus_mode = str(mode).upper() if mode is not None else None
        self.data.modbus_control = _enabled_state(ctr.get("on"))
        self.data.sunspec_mode = slave.get("sunspecMode")
        self.data.modbus_restriction = _enabled_state(restriction.get("on"))
        self.data.modbus_restriction_ip = restriction.get("ip")

    def _set_effective_battery_mode(
        self, raw_mode: int | None, raw_soc_mode: str | None
    ) -> None:
        effective_mode = _derive_api_battery_mode(raw_mode)
        self.data.battery_mode_raw = raw_mode
        self.data.battery_mode_effective = effective_mode
        self.data.battery_mode = (
            API_BATTERY_MODE.get(effective_mode) if effective_mode is not None else None
        )
        self.data.soc_mode_raw = raw_soc_mode
        self.data.soc_mode = API_SOC_MODE.get(raw_soc_mode, raw_soc_mode)

    async def async_refresh(self) -> WebData:
        """Poll the web API and return a snapshot of the resulting state.

        Under the write lock: a poll started before a write finished would
        otherwise overwrite the confirmed new state with the older reading.
        """
        async with self._write_lock:
            return await self._async_refresh_locked()

    async def _async_refresh_locked(self) -> WebData:
        if not self._client:
            return replace(self.data)

        inverter_info = await self._async_client_job("get_inverter_info")
        self.data.inverter_temperature = (
            inverter_info.get("temperature")
            if isinstance(inverter_info, dict)
            else None
        )

        modbus_config = await self._async_client_job("get_modbus_config")
        if isinstance(modbus_config, dict):
            self._apply_web_modbus_config(modbus_config)

        solar_api_config = await self._async_client_job("get_solar_api_config")
        if isinstance(solar_api_config, dict):
            enabled = solar_api_config.get("SolarAPIv1Enabled")
            self.data.solar_api_enabled = (
                _enabled_bool(enabled) if enabled is not None else None
            )
        else:
            self.data.solar_api_enabled = None

        if self._storage_present:
            storage_info = await self._async_client_job("get_storage_info")
            if isinstance(storage_info, dict):
                self.data.storage_temperature = storage_info.get("cell_temperature")
                self.data.storage_manufacturer = storage_info.get("manufacturer")
                self.data.storage_model = storage_info.get("model")
                self.data.storage_serial = storage_info.get("serial")
            else:
                self.data.storage_temperature = None

            battery_config = await self._async_client_job("get_battery_config")
            if isinstance(battery_config, dict):
                self._apply_web_battery_config(battery_config)

        if self._technician_client:
            export_limit_config = await self._async_tech_web_job(
                self._technician_client.get_export_limit_config
            )
        elif self._client:
            export_limit_config = await self._async_web_job(
                self._client.get_export_limit_config
            )
        else:
            export_limit_config = None
        _LOGGER.debug(
            "Export limit config from web API: %s",
            _export_limit_summary(export_limit_config),
        )
        self.data.export_soft_limit_w = None
        if isinstance(export_limit_config, dict) and export_limit_config:
            soft = (
                export_limit_config.get("exportLimits", {})
                .get("activePower", {})
                .get("softLimit", {})
            )
            if isinstance(soft, dict) and soft.get("enabled"):
                self.data.export_soft_limit_w = soft.get("powerLimit")

        self._async_sync_solar_api_warning()
        return replace(self.data)

    # -- battery write transition -------------------------------------------------

    def _schedule_delayed_web_refresh(self) -> None:
        if self._delayed_refresh_task and not self._delayed_refresh_task.done():
            self._delayed_refresh_task.cancel()

        async def delayed_refresh() -> None:
            try:
                await asyncio.sleep(BATTERY_WRITE_WEB_REFRESH_DELAY_SECONDS)
                if self._client:
                    await self.async_refresh()
                    if self._coordinator is not None:
                        self._coordinator.async_set_updated_data(replace(self.data))
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Delayed Fronius web API refresh failed: %s", err)

        self._delayed_refresh_task = self._hass.loop.create_task(delayed_refresh())

    def _start_battery_write_transition(self, source: str) -> None:
        self._on_battery_write()
        self._schedule_delayed_web_refresh()
        _LOGGER.debug(
            "Started Modbus recovery window after %s write for %s", source, self._host
        )

    # -- setters -----------------------------------------------------------------

    @_serialised
    async def set_solar_api_enabled(self, enabled: bool) -> None:
        """Enable or disable the Solar API."""
        if not self._client:
            raise RuntimeError(WEB_API_NOT_CONFIGURED)

        await self._async_web_job(
            self._client.set_solar_api_enabled, enabled, raise_on_auth_failure=True
        )
        self.data.solar_api_enabled = bool(enabled)
        self._async_sync_solar_api_warning()

    @_serialised
    async def reset_modbus_control(self) -> None:
        """Reset the inverter's Modbus configuration to its defaults."""
        if not self._client:
            raise RuntimeError(WEB_API_NOT_CONFIGURED)

        await self._async_web_job(
            self._client.reset_modbus_control, raise_on_auth_failure=True
        )

    def _get_next_soc_limits(
        self, *, soc_min: int | None = None, soc_max: int | None = None
    ) -> tuple[int, int]:
        next_soc_min = self.data.soc_min if soc_min is None else int(soc_min)
        next_soc_max = self.data.soc_max if soc_max is None else int(soc_max)

        next_soc_min = SOC_LOWEST if next_soc_min is None else next_soc_min
        next_soc_max = SOC_MAX_DEFAULT if next_soc_max is None else next_soc_max

        if next_soc_min < SOC_LOWEST or next_soc_min > SOC_HIGHEST:
            raise ValueError("SoC Minimum must be between 5 and 100")
        if next_soc_max < 0 or next_soc_max > SOC_HIGHEST:
            raise ValueError("SoC Maximum must be between 0 and 100")
        if next_soc_min > next_soc_max:
            raise ValueError("SoC Minimum must not exceed SoC Maximum")

        return next_soc_min, next_soc_max

    def _get_api_soc_values(
        self, *, soc_min: int | None = None, soc_max: int | None = None
    ) -> tuple[int, int, int]:
        next_soc_min, next_soc_max = self._get_next_soc_limits(
            soc_min=soc_min, soc_max=soc_max
        )
        next_backup_reserved = self.data.backup_reserved
        next_backup_reserved = (
            SOC_LOWEST if next_backup_reserved is None else next_backup_reserved
        )
        if next_backup_reserved < SOC_LOWEST or next_backup_reserved > SOC_HIGHEST:
            raise ValueError("Battery backup reserve must be between 5 and 100")

        return next_soc_min, next_soc_max, next_backup_reserved

    def validate_soc_minimum(self, soc_min: int) -> None:
        """Raise if the web API would reject this minimum, before anything is written."""
        self._get_api_soc_values(soc_min=soc_min)

    def _require_battery_mode_manual(self, control_name: str) -> None:
        if not self.battery_mode_is_manual:
            raise ValueError(
                f"{control_name} can only be changed when Battery API mode is Manual"
            )

    async def _set_api_soc_manual(
        self,
        soc_min: int | None = None,
        soc_max: int | None = None,
        control_name: str = "SoC Maximum",
    ) -> tuple[int, int, int] | None:
        if not self._client:
            raise RuntimeError(WEB_API_NOT_CONFIGURED)
        self._require_battery_mode_manual(control_name)

        next_soc_min, next_soc_max, next_backup_reserved = self._get_api_soc_values(
            soc_min=soc_min, soc_max=soc_max
        )
        await self._async_web_job(
            self._client.set_battery_soc_config,
            next_soc_min,
            next_soc_max,
            next_backup_reserved,
            raise_on_auth_failure=True,
        )
        self._set_effective_battery_mode(BATTERY_MODE_MANUAL, SOC_MODE_MANUAL)
        self.data.soc_min = next_soc_min
        self.data.soc_max = next_soc_max
        self.data.backup_reserved = next_backup_reserved
        self._start_battery_write_transition(control_name)
        return next_soc_min, next_soc_max, next_backup_reserved

    @_serialised
    async def set_battery_mode(self, mode: int) -> None:
        """Switch the battery between Auto (0) and Manual (1) control."""
        if not self._client:
            raise RuntimeError(WEB_API_NOT_CONFIGURED)

        current_effective_mode = self.data.battery_mode_effective
        display_power = self.data.battery_power_w
        if mode == BATTERY_MODE_MANUAL and display_power is None:
            display_power = 0
        power = (
            -display_power
            if mode == BATTERY_MODE_MANUAL and display_power is not None
            else None
        )
        soc_min = None
        if (
            mode == BATTERY_MODE_MANUAL
            and current_effective_mode != BATTERY_MODE_MANUAL
        ):
            # Leaving Manual resets the web API's own minimum to SOC_LOWEST, so the
            # Modbus reserve is the only record of what the user actually wants.
            modbus_reserve = self._modbus_soc_minimum()
            soc_min = self.data.soc_min if modbus_reserve is None else modbus_reserve

        await self._async_web_job(
            self._client.set_battery_config,
            mode,
            power,
            soc_min,
            raise_on_auth_failure=True,
        )
        self._set_effective_battery_mode(
            mode, SOC_MODE_MANUAL if mode == BATTERY_MODE_MANUAL else SOC_MODE_AUTO
        )
        if mode == BATTERY_MODE_MANUAL:
            self.data.battery_power_w = display_power
            if soc_min is not None:
                self.data.soc_min = soc_min
        else:
            self.data.soc_min = SOC_LOWEST
            self.data.soc_max = SOC_HIGHEST
        self._start_battery_write_transition("Battery API mode")

    @_serialised
    async def set_battery_power_w(self, value: float) -> None:
        """Set the target feed-in power in Manual battery mode."""
        if not self._client:
            raise RuntimeError(WEB_API_NOT_CONFIGURED)
        self._require_battery_mode_manual("Target feed in")

        power = -int(round(value))
        await self._async_web_job(
            self._client.set_battery_config,
            BATTERY_MODE_MANUAL,
            power,
            raise_on_auth_failure=True,
        )
        self.data.battery_power_w = int(round(value))
        self._set_effective_battery_mode(BATTERY_MODE_MANUAL, SOC_MODE_MANUAL)
        self._start_battery_write_transition("Target feed in")

    @_serialised
    async def set_soc_maximum(self, soc_max: int) -> None:
        """Set the maximum state of charge in Manual battery mode."""
        if not self._client:
            raise RuntimeError(WEB_API_NOT_CONFIGURED)
        await self._set_api_soc_manual(soc_max=soc_max, control_name="SoC Maximum")

    @_serialised
    async def set_soc_minimum_manual(self, soc_min: int) -> None:
        """Set the minimum state of charge in Manual battery mode."""
        if not self._client:
            raise RuntimeError(WEB_API_NOT_CONFIGURED)
        await self._set_api_soc_manual(soc_min=soc_min, control_name="SoC Minimum")

    @_serialised
    async def set_charge_sources(
        self,
        *,
        charge_from_grid: bool | None = None,
        charge_from_ac: bool | None = None,
    ) -> None:
        """Allow charging the battery from the grid and/or from AC."""
        if not self._client:
            raise RuntimeError(WEB_API_NOT_CONFIGURED)

        if charge_from_ac is False:
            next_charge_from_grid = False
            next_charge_from_ac = False
        else:
            next_charge_from_grid = (
                _enabled_bool(self.data.charge_from_grid)
                if charge_from_grid is None
                else bool(charge_from_grid)
            )
            next_charge_from_ac = (
                _enabled_bool(self.data.charge_from_ac)
                if charge_from_ac is None
                else bool(charge_from_ac)
            )
            if next_charge_from_grid and charge_from_ac is None:
                next_charge_from_ac = True

        await self._async_web_job(
            self._client.set_battery_charge_sources,
            next_charge_from_grid,
            next_charge_from_ac,
            raise_on_auth_failure=True,
        )
        self.data.charge_from_grid = next_charge_from_grid
        self.data.charge_from_ac = next_charge_from_ac
        self._start_battery_write_transition("battery charge source")

    @_serialised
    async def set_export_soft_limit_w(self, value: float) -> None:
        """Set the export soft limit; requires technician credentials."""
        if not self._technician_client:
            raise RuntimeError(TECHNICIAN_NOT_CONFIGURED)
        limit_w = int(round(value))
        result = await self._async_tech_web_job(
            self._technician_client.set_export_soft_limit,
            limit_w,
            raise_on_auth_failure=True,
        )
        if not result:
            raise RuntimeError("The inverter did not accept the export soft limit")
        self.data.export_soft_limit_w = limit_w
        if self._coordinator is not None:
            self._coordinator.async_set_updated_data(replace(self.data))
