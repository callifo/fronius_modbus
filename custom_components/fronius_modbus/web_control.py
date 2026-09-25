"""The web-API half of the integration: readings and battery controls Modbus does not offer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
import functools
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .component_readings import flag_value
from .const import (
    API_BATTERY_MODE,
    API_SOC_MODE,
    DEFAULT_METER_UNIT_ID,
    DOMAIN,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
    SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX,
    TECHNICIAN_USERNAME,
)
from .fronius_modbus_api.exceptions import ControlRefused, ControlUnavailable
from .froniuswebclient import FroniusWebAuthError, FroniusWebClient, is_enabled
from .token_store import async_get_token_store

_LOGGER = logging.getLogger(__name__)
WEB_API_NOT_CONFIGURED = "Fronius Web API is not configured"
TECHNICIAN_NOT_CONFIGURED = (
    "Technician access is not selected - choose the technician role via Configure"
)


def _serialised(method):
    """Run one web write at a time: every setter reads state, composes a payload and writes it back."""

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with self._write_lock:
            return await method(self, *args, **kwargs)

    return wrapper


def _last_writer_wins(method):
    """Serialise like _serialised, but a burst reaches the device as its first and last value.

    Every call takes a ticket; whoever holds the lock while a newer ticket
    exists returns without writing, because that newer call carries the value
    now. Each write the inverter takes costs it seconds of Modbus, so ten steps
    of one control must not become ten writes. The last caller still gets the
    last write's error.
    """

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        ticket = self._tickets[method.__name__] = (
            self._tickets.get(method.__name__, 0) + 1
        )
        async with self._write_lock:
            if self._tickets[method.__name__] != ticket:
                return None
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
SOC_LOWEST, SOC_HIGHEST = 5, 100


def _implied_charge_sources(
    grid: bool | None, from_ac: bool | None
) -> tuple[bool | None, bool | None]:
    """The flags a request sets: itself and what the inverter ties to it.

    Grid charging needs AC charging, so switching AC off takes grid with it
    and switching grid on takes AC with it. The other flag is left alone: taken
    from the last poll, it undid a change made since (audit F24-01).
    """
    if from_ac is False:
        return False, False
    if grid:
        return True, True
    return grid, from_ac


@dataclass
class WebData:
    """The web API's contribution to the entities' state, refreshed on its own schedule."""

    modbus_mode: str | None = None
    modbus_control: str | None = None
    sunspec_mode: str | None = None
    modbus_restriction: str | None = None
    modbus_restriction_ip: str | None = None
    solar_api_enabled: bool | None = None
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
    # None while the component endpoint has not answered: unread, not absent.
    inverter_readings: dict[str, Any] | None = None
    storage_readings: dict[str, Any] | None = None
    # The endpoint answered 404: firmware without it, so no component sensors.
    inverter_endpoint_missing: bool = False
    storage_endpoint_missing: bool = False


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
    return "enabled" if is_enabled(value) else "disabled"


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


@dataclass(slots=True)
class MeterTopology:
    """Which meters the web API reports, and whether it actually answered.

    `confirmed` is the difference between "this device has one meter" and "the
    question could not be asked": only the former may retire entities (audit A04).
    """

    unit_ids: list[int]
    primary_unit_id: int
    locations: dict[int, int]
    confirmed: bool


def _config_part(config: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    """A nested part; None when it, or what holds it, has the wrong shape."""
    if config is None:
        return None
    part = config.get(key, {})
    return part if isinstance(part, dict) else None


def _flag_state(part: dict[str, Any] | None) -> str | None:
    """An unreadable flag is unknown, not "disabled" (audit RR770-03, R730-03)."""
    if part is None:
        return None
    value = part.get("on")
    if value is None:
        return _enabled_state(value)
    flag = flag_value(value)
    if flag is None:
        return None
    return _enabled_state(flag)


def parse_meter_topology(info: dict | None) -> MeterTopology:
    """The topology an answer describes, or the unconfirmed single-meter default."""
    default = MeterTopology([DEFAULT_METER_UNIT_ID], DEFAULT_METER_UNIT_ID, {}, False)
    if not info or not info.get("unit_ids"):
        return default
    unit_ids = [int(unit) for unit in info["unit_ids"] if int(unit) > 0]
    if not unit_ids:
        return default
    return MeterTopology(
        unit_ids=unit_ids,
        primary_unit_id=int(info.get("primary_unit_id") or unit_ids[0]),
        locations={
            int(unit_id): int(location)
            for unit_id, location in (info.get("locations_by_unit_id") or {}).items()
        },
        confirmed=True,
    )


class FroniusWebControl:
    """Solar-API state and battery/export controls, alongside the Modbus poll."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry | None,
        *,
        host: str,
        client: FroniusWebClient | None,
        api_username: str,
        storage_present: bool,
        inverter_firmware: Callable[[], str | None],
        on_battery_write: Callable[[], None],
        public_client: FroniusWebClient | None = None,
    ) -> None:
        """Bind to the web clients; on_battery_write opens the Modbus recovery window."""
        self._hass = hass
        self._entry = entry
        self._host = host
        self._client = client
        # The component and meter endpoints answer without a login: they stay
        # readable when there is none, or once it is lost.
        self._public_client = public_client or client
        self._api_username = api_username
        self._storage_present = storage_present
        self._inverter_firmware = inverter_firmware
        self._on_battery_write = on_battery_write
        # One write at a time (audit F10): concurrent read-modify-write of the
        # SoC tuple or the charge-source pair lost one of the two changes.
        self._write_lock = asyncio.Lock()
        self._tickets: dict[str, int] = {}
        self.data = WebData()
        self._coordinator: Any = None
        self._delayed_refresh_task: asyncio.Task | None = None

    def attach_coordinator(self, coordinator: Any) -> None:
        """Bind the web coordinator so the delayed post-write refresh can push new data."""
        self._coordinator = coordinator

    async def async_meter_topology(self) -> MeterTopology:
        """Ask the web API which meters exist; unconfirmed when it could not be read."""
        return parse_meter_topology(
            await self._async_client_job(
                "get_power_meter_info", DEFAULT_METER_UNIT_ID, public=True
            )
        )

    @property
    def configured(self) -> bool:
        """Whether the customer web API client is available."""
        return self._client is not None

    @property
    def technician_configured(self) -> bool:
        """Whether the one configured login is the technician role."""
        return self.configured and self._api_username == TECHNICIAN_USERNAME

    @property
    def battery_mode_is_manual(self) -> bool:
        """Whether self-consumption optimisation (HYB_EM_MODE) is confirmed Manual."""
        return self.data.battery_mode_effective == BATTERY_MODE_MANUAL

    @property
    def soc_mode_is_manual(self) -> bool:
        """Whether the SoC window (BAT_M0_SOC_MODE) is under manual control.

        The inverter keeps this switch apart from self-consumption optimisation,
        so the SoC limits must follow it and not HYB_EM_MODE.
        """
        return self.data.soc_mode_raw == SOC_MODE_MANUAL

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
                raise ControlUnavailable(
                    "web_api_not_configured", WEB_API_NOT_CONFIGURED
                )
            return None

        try:
            return await self._hass.async_add_executor_job(func, *args)
        except FroniusWebAuthError as err:
            await self._async_handle_web_api_auth_failure(err)
            if raise_on_auth_failure:
                raise ControlUnavailable(
                    "web_api_auth_failed",
                    "Fronius Web API authentication failed. Reconfigure the integration.",
                ) from err
            return None

    async def _async_client_job(self, method_name: str, *args, public: bool = False):
        """Run one refresh step, re-reading the client: an auth failure clears it mid-refresh."""
        client = self._public_client if public else self._client
        if client is None:
            return None
        try:
            return await self._hass.async_add_executor_job(
                getattr(client, method_name), *args
            )
        except FroniusWebAuthError as err:
            await self._async_handle_web_api_auth_failure(err)
            return None

    async def _async_handle_web_api_auth_failure(self, err: Exception) -> None:
        if not self._client:
            return

        _LOGGER.warning("Disabling the Fronius web API after an auth failure: %s", err)
        self._client = None
        self.data = WebData()
        await async_get_token_store(self._hass).async_delete_token(
            self._host, self._api_username
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
        self.data.charge_from_ac = flag_value(battery_config.get("HYB_BM_CHARGEFROMAC"))
        self.data.charge_from_grid = flag_value(
            battery_config.get("HYB_EVU_CHARGEFROMGRID")
        )

    def _apply_storage_info(self, storage_info: Any) -> None:
        # An unread identity keeps the last one: the device entry would flicker.
        if not isinstance(storage_info, dict):
            self.data.storage_readings = None
            return
        self.data.storage_readings = storage_info.get("readings")
        self.data.storage_endpoint_missing = bool(storage_info.get("missing"))
        # Without the device node the identity is the parser's placeholder,
        # not a reading (audit FA0FB-07).
        if self.data.storage_readings is None:
            return
        self.data.storage_manufacturer = storage_info.get("manufacturer")
        self.data.storage_model = storage_info.get("model")
        self.data.storage_serial = storage_info.get("serial")

    def _apply_web_modbus_config(self, modbus_config: dict[str, Any]) -> None:
        # Shown only: a part of the wrong shape is unknown, not a failed poll
        # that takes every other web value down with it (audit FA0FB-06).
        slave = _config_part(modbus_config, "slave")
        ctr = _config_part(slave, "ctr")
        restriction = _config_part(ctr, "restriction")
        mode = (slave or {}).get("mode")

        self.data.modbus_mode = str(mode).upper() if mode is not None else None
        self.data.modbus_control = _flag_state(ctr)
        self.data.sunspec_mode = (slave or {}).get("sunspecMode")
        self.data.modbus_restriction = _flag_state(restriction)
        self.data.modbus_restriction_ip = (restriction or {}).get("ip")

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
        if not self._public_client:
            return replace(self.data)

        inverter_info = await self._async_client_job("get_inverter_info", public=True)
        inverter = inverter_info if isinstance(inverter_info, dict) else {}
        self.data.inverter_readings = inverter.get("readings")
        self.data.inverter_endpoint_missing = bool(inverter.get("missing"))

        modbus_config = await self._async_client_job("get_modbus_config")
        if isinstance(modbus_config, dict):
            self._apply_web_modbus_config(modbus_config)

        solar_api_config = await self._async_client_job("get_solar_api_config")
        if isinstance(solar_api_config, dict):
            enabled = solar_api_config.get("SolarAPIv1Enabled")
            self.data.solar_api_enabled = flag_value(enabled)
        else:
            self.data.solar_api_enabled = None

        if self._storage_present:
            self._apply_storage_info(
                await self._async_client_job("get_storage_info", public=True)
            )

            battery_config = await self._async_client_job("get_battery_config")
            if isinstance(battery_config, dict):
                self._apply_web_battery_config(battery_config)

        export_limit_config = await self._async_client_job("get_export_limit_config")
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
            if isinstance(soft, dict) and flag_value(soft.get("enabled")) is True:
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
                    self._publish()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Delayed Fronius web API refresh failed: %s", err)

        self._delayed_refresh_task = self._hass.loop.create_task(delayed_refresh())

    def _after_battery_write(self, source: str, *, written: bool) -> None:
        """Publish the new state; only a write that reached the inverter opens the window."""
        if not written:
            self._publish()
            return
        self._on_battery_write()
        self._publish()
        self._schedule_delayed_web_refresh()
        _LOGGER.debug("Started the Modbus recovery window after a %s write", source)

    def _publish(self) -> None:
        """Hand the entities the state a write just produced.

        The delayed refresh confirms it minutes later; until then the entity
        would show the write undone (discussion #6).
        """
        if self._coordinator is not None:
            self._coordinator.async_set_updated_data(replace(self.data))

    # -- setters -----------------------------------------------------------------

    @_serialised
    async def set_solar_api_enabled(self, enabled: bool) -> None:
        """Enable or disable the Solar API."""
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)

        await self._async_web_job(
            self._client.set_solar_api_enabled, enabled, raise_on_auth_failure=True
        )
        self.data.solar_api_enabled = bool(enabled)
        self._publish()
        self._async_sync_solar_api_warning()

    @_serialised
    async def reset_modbus_control(self) -> None:
        """Reset the inverter's Modbus configuration to its defaults."""
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)

        await self._async_web_job(
            self._client.reset_modbus_control, raise_on_auth_failure=True
        )

    @staticmethod
    def _check_soc_range(
        soc_min: int | None = None, soc_max: int | None = None
    ) -> None:
        """Only the limit asked for; the window is checked against the inverter.

        A check against the last poll refused a window the inverter would take
        and passed one it refuses (reaudit RE26-02).
        """
        if soc_min is not None and not SOC_LOWEST <= soc_min <= SOC_HIGHEST:
            raise ControlRefused(
                "value_out_of_range",
                "SoC Minimum must be between 5 and 100",
                minimum=str(SOC_LOWEST),
                maximum=str(SOC_HIGHEST),
            )
        if soc_max is not None and not 0 <= soc_max <= SOC_HIGHEST:
            raise ControlRefused(
                "value_out_of_range",
                "SoC Maximum must be between 0 and 100",
                minimum="0",
                maximum=str(SOC_HIGHEST),
            )

    @_last_writer_wins
    async def apply_soc_minimum(
        self, soc_min: int, write_modbus: Callable[[], Awaitable[None]]
    ) -> None:
        """Write the shared minimum to Modbus and, in manual SoC mode, to the web API.

        Both writes and the check that guards them happen under this lock. A
        check outside it went stale when a concurrent maximum change landed in
        between, and Modbus then took a reserve the web API refuses (audit A05).
        The window is read from the inverter before Modbus is written: checked
        only at the web write, Modbus already held a refused minimum (RE26-02).
        """
        client = self._client
        if client is None or not self.soc_mode_is_manual:
            await write_modbus()
            return
        self._check_soc_range(soc_min=soc_min)
        await self._async_web_job(
            client.check_soc_window, soc_min, raise_on_auth_failure=True
        )
        await write_modbus()
        try:
            await self._set_api_soc_manual(soc_min=soc_min, control_name="SoC Minimum")
        except (RuntimeError, ValueError, OSError) as err:
            # Modbus holds the new minimum and keeps it; the owner has to know
            # the two minimums now differ (audit FA0FB-04).
            raise ControlUnavailable(
                "soc_minimum_web_failed",
                f"The Modbus reserve is set, but the web API refused it: {err}",
                error=str(err),
            ) from err

    def _require_battery_mode_manual(self, control_name: str) -> None:
        if not self.battery_mode_is_manual:
            raise ControlRefused(
                "needs_manual_self_consumption",
                f"{control_name} can only be changed when self-consumption "
                "optimisation is Manual",
            )

    def _require_soc_mode_manual(self, control_name: str) -> None:
        if not self.soc_mode_is_manual:
            raise ControlRefused(
                "needs_manual_soc_mode",
                f"{control_name} can only be changed when the SoC mode is Manual",
            )

    async def _set_api_soc_manual(
        self,
        soc_min: int | None = None,
        soc_max: int | None = None,
        control_name: str = "SoC Maximum",
    ) -> None:
        """Send the one limit asked for; the other stays as the inverter holds it.

        The client checks the window against a fresh read. Sending the cached
        other limit undid one set in the inverter UI since the last poll
        (audit F24-01).
        """
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)
        self._require_soc_mode_manual(control_name)
        self._check_soc_range(soc_min=soc_min, soc_max=soc_max)

        written = await self._async_web_job(
            self._client.set_soc_limits,
            soc_min,
            soc_max,
            raise_on_auth_failure=True,
        )
        if soc_min is not None:
            self.data.soc_min = int(soc_min)
        if soc_max is not None:
            self.data.soc_max = int(soc_max)
        self._after_battery_write(control_name, written=written)

    @_serialised
    async def set_battery_mode(self, mode: int) -> None:
        """Switch self-consumption optimisation between Auto (0) and Manual (1).

        The SoC window is not touched: the inverter keeps its own switch for it.
        """
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)

        # The mode alone: the target stays as the inverter holds it (audit F24-01).
        written = await self._async_web_job(
            self._client.set_battery_config, mode, raise_on_auth_failure=True
        )
        self._set_effective_battery_mode(mode, self.data.soc_mode_raw)
        self._after_battery_write("self-consumption optimisation", written=written)

    @_last_writer_wins
    async def set_battery_power_w(self, value: float) -> None:
        """Set the target feed-in power in manual self-consumption optimisation."""
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)
        self._require_battery_mode_manual("Target feed in")

        written = await self._async_web_job(
            self._client.set_battery_power,
            -int(round(value)),
            raise_on_auth_failure=True,
        )
        self.data.battery_power_w = int(round(value))
        self._after_battery_write("Target feed in", written=written)

    @_last_writer_wins
    async def set_soc_maximum(self, soc_max: int) -> None:
        """Set the maximum state of charge in manual SoC mode."""
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)
        await self._set_api_soc_manual(soc_max=soc_max, control_name="SoC Maximum")

    @_last_writer_wins
    async def set_soc_minimum_manual(self, soc_min: int) -> None:
        """Set the web API's minimum state of charge in manual SoC mode."""
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)
        await self._set_api_soc_manual(soc_min=soc_min, control_name="SoC Minimum")

    @_serialised
    async def set_soc_mode(self, *, manual: bool) -> None:
        """Switch the SoC window between the inverter's automatic and manual control."""
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)
        mode = SOC_MODE_MANUAL if manual else SOC_MODE_AUTO
        written = await self._async_web_job(
            self._client.set_soc_mode, mode, raise_on_auth_failure=True
        )
        self._set_effective_battery_mode(self.data.battery_mode_raw, mode)
        self._after_battery_write("SoC mode", written=written)

    @_last_writer_wins
    async def set_backup_reserve(self, percent: int) -> None:
        """Set the backup power reserve; the inverter offers it in either battery mode."""
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)
        if percent < SOC_LOWEST or percent > SOC_HIGHEST:
            raise ControlRefused(
                "value_out_of_range",
                "Battery backup reserve must be between 5 and 100",
                minimum=str(SOC_LOWEST),
                maximum=str(SOC_HIGHEST),
            )
        written = await self._async_web_job(
            self._client.set_backup_reserve, percent, raise_on_auth_failure=True
        )
        self.data.backup_reserved = percent
        self._after_battery_write("Backup reserve", written=written)

    @_serialised
    async def set_charge_sources(
        self,
        *,
        charge_from_grid: bool | None = None,
        charge_from_ac: bool | None = None,
    ) -> None:
        """Allow charging the battery from the grid and/or from AC."""
        if not self._client:
            raise ControlUnavailable("web_api_not_configured", WEB_API_NOT_CONFIGURED)

        grid, from_ac = _implied_charge_sources(charge_from_grid, charge_from_ac)
        written = await self._async_web_job(
            self._client.set_battery_charge_sources,
            grid,
            from_ac,
            raise_on_auth_failure=True,
        )
        if grid is not None:
            self.data.charge_from_grid = grid
        if from_ac is not None:
            self.data.charge_from_ac = from_ac
        self._after_battery_write("battery charge source", written=written)

    @_last_writer_wins
    async def set_export_soft_limit_w(self, value: float) -> None:
        """Set the export soft limit; only the technician role may write it."""
        client = self._client
        if client is None or not self.technician_configured:
            raise ControlUnavailable(
                "technician_not_configured", TECHNICIAN_NOT_CONFIGURED
            )
        limit_w = int(round(value))
        await self._async_web_job(
            client.set_export_soft_limit,
            limit_w,
            raise_on_auth_failure=True,
        )
        self.data.export_soft_limit_w = limit_w
        self._publish()
