"""The two pollers: the Modbus device on the scan interval, the web API on its own."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import TYPE_CHECKING, cast

from modbus_connection import ModbusError, ModbusTimeoutError
from modbus_connection.model.sunspec import SunSpecMapShiftError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .derived import LoadEstimator, grid_status
from .fronius_modbus_api.controls import InverterControls
from .fronius_modbus_api.device import (
    REPORT_INVERTER,
    REPORT_MPPT,
    FroniusInverter,
    UpdateReport,
    meter_report_name,
)
from .fronius_modbus_api.storage import StorageControl
from .fronius_modbus_api.sunspec_models import AcMeter

if TYPE_CHECKING:
    from .web_control import FroniusWebControl, WebData

_LOGGER = logging.getLogger(__name__)


def assume_present[Subsystem](subsystem: Subsystem | None) -> Subsystem:
    """A sub-system the caller already established, typed as present.

    The entity tables and the derived values reach straight through the
    optional sub-systems, which stay ``X | None`` until the device reports
    them. Narrowing here would only move the failure one frame, so this is a
    type-level statement and nothing else.
    """
    return cast(Subsystem, subsystem)


# A link that stays up but stops answering: after this many timeouts in a row the
# socket is recycled so the next poll opens a fresh one.
TIMEOUTS_BEFORE_RECYCLE = 3
DEFAULT_MAX_RATE_W = 11000  # what 0.3 assumed before the nameplate was read


@dataclass
class ModbusPoll:
    """One Modbus poll as the entities see it."""

    report: UpdateReport
    load_w: float | None
    grid_status: str | None


@dataclass
class FroniusRuntimeData:
    """What a loaded entry carries."""

    device: FroniusInverter
    modbus: FroniusModbusCoordinator
    web: FroniusWebCoordinator | None
    web_control: FroniusWebControl | None
    meter_locations: dict[int, int] = field(default_factory=dict)
    primary_meter_unit_id: int = 200

    @property
    def storage_control(self) -> StorageControl | None:
        """The storage control built on the modbus coordinator's first good poll."""
        return self.modbus.storage_control

    @property
    def inverter_controls(self) -> InverterControls | None:
        """The inverter controls built on the modbus coordinator's first good poll."""
        return self.modbus.inverter_controls

    @property
    def web_data(self) -> WebData | None:
        """The web coordinator's last data, or None when the web API is not set up."""
        return None if self.web is None else self.web.data


type FroniusConfigEntry = ConfigEntry[FroniusRuntimeData]


class FroniusModbusCoordinator(DataUpdateCoordinator[ModbusPoll]):
    """Polls the inverter and its meters; builds the control objects on the first good poll."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: FroniusInverter,
        *,
        interval: timedelta,
        primary_meter_unit_id: int,
        meter_locations: dict[int, int],
    ) -> None:
        """Bind to the device and the entry the poll results are reloaded through."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{entry.title} Modbus",
            update_interval=interval,
        )
        self.device = device
        self.storage_control: StorageControl | None = None
        self.inverter_controls: InverterControls | None = None
        self._primary_meter_unit_id = primary_meter_unit_id
        self._meter_locations = meter_locations
        self._load = LoadEstimator()
        self._failed: frozenset[str] = frozenset()
        # Sub-systems the platforms were built for: the ones that had answered
        # by the first refresh. One that answers only later has no entities
        # yet, so the entry reloads once to build them (audit F03).
        self._built_for: frozenset[str] | None = None
        self._timeouts = 0
        self._tolerate_until = 0.0
        self._tolerated_failures = 0

    def tolerate_failures_until(self, monotonic_deadline: float) -> None:
        """Keep the last values through the outage a web battery write causes."""
        self._tolerate_until = monotonic_deadline

    async def _async_update_data(self) -> ModbusPoll:
        try:
            report = await self.device.async_update()
        except SunSpecMapShiftError as err:
            self.hass.config_entries.async_schedule_reload(
                assume_present(self.config_entry).entry_id
            )
            raise UpdateFailed(f"SunSpec map changed, reloading: {err}") from err
        except ModbusTimeoutError as err:
            self._timeouts += 1
            if self._timeouts >= TIMEOUTS_BEFORE_RECYCLE:
                await self.device.unit.disconnect()
                self._timeouts = 0
            return await self._failed_poll(err)
        except ModbusError as err:
            return await self._failed_poll(err)
        self._timeouts = 0
        self._tolerate_until = 0.0
        self._tolerated_failures = 0
        if not report.updated:
            raise UpdateFailed("no sub-system answered")
        for name in sorted(report.failed.keys() - self._failed):
            _LOGGER.warning("Failed to read %s: %s", name, report.failed[name])
        self._failed = frozenset(report.failed)
        self._reload_for_new_sub_systems(report)
        self._build_controls()
        return ModbusPoll(
            report=report,
            load_w=self._load_w(report),
            grid_status=self._grid_status(report),
        )

    async def _failed_poll(self, err: ModbusError) -> ModbusPoll:
        if self.data is not None and time.monotonic() < self._tolerate_until:
            log = _LOGGER.warning if self._tolerated_failures == 0 else _LOGGER.debug
            log("Modbus outage tolerated after a web write: %s", err)
            self._tolerated_failures += 1
            return self.data
        if self._tolerated_failures:
            # 0.3 closed the client when the window opened. The connection is
            # shared with the rest of Home Assistant now, so it may not be
            # dropped preemptively - recycle it once the window ends still
            # failing, which is when a stale socket is the likely cause.
            self._tolerated_failures = 0
            self._tolerate_until = 0.0
            await self.device.unit.disconnect()
        raise UpdateFailed(f"Modbus communication failure: {err}") from err

    def _reload_for_new_sub_systems(self, report: UpdateReport) -> None:
        answered = frozenset(report.updated)
        if self._built_for is None:
            self._built_for = answered
            return
        new = answered - self._built_for
        if not new:
            return
        _LOGGER.info(
            "%s answered for the first time; reloading the entry to add its entities",
            ", ".join(sorted(new)),
        )
        self._built_for = self._built_for | new
        self.hass.config_entries.async_schedule_reload(
            assume_present(self.config_entry).entry_id
        )

    def _build_controls(self) -> None:
        device = self.device
        self._refresh_storage_control()
        if self.inverter_controls is None and device.controls is not None:
            self.inverter_controls = InverterControls(device.controls, max_power_w=None)
        if self.inverter_controls is not None and device.settings is not None:
            self.inverter_controls.max_power_w = device.settings.w_max

    def _refresh_storage_control(self) -> None:
        """Build the storage control once, then keep its rate maxima current.

        A first poll without model 120 built it on the fallback maximum; every
        later nameplate read refreshes it (audit F09), the same way
        max_power_w follows the settings.
        """
        device = self.device
        if device.storage is None:
            return
        nameplate = device.nameplate
        max_charge = (
            nameplate.max_cha_rte if nameplate else None
        ) or DEFAULT_MAX_RATE_W
        max_discharge = (
            nameplate.max_dis_cha_rte if nameplate else None
        ) or DEFAULT_MAX_RATE_W
        if self.storage_control is None:
            self.storage_control = StorageControl(
                device.storage,
                max_charge_rate_w=max_charge,
                max_discharge_rate_w=max_discharge,
            )
        else:
            self.storage_control.max_charge_rate_w = max_charge
            self.storage_control.max_discharge_rate_w = max_discharge
        self.storage_control.sync_from_device()

    def _primary_meter(self) -> AcMeter | None:
        info = self.device.meters.get(self._primary_meter_unit_id)
        return None if info is None else info.meter

    def _primary_meter_fresh(self, report: UpdateReport) -> AcMeter | None:
        """The primary meter, only when both it and the inverter answered this poll.

        Every derived value mixes the two; a stale half would pass as current
        (audit F14).
        """
        meter = self._primary_meter()
        if (
            meter is None
            or meter_report_name(self._primary_meter_unit_id) not in report.updated
            or REPORT_INVERTER not in report.updated
        ):
            return None
        return meter

    def _load_w(self, report: UpdateReport) -> float | None:
        meter = self._primary_meter_fresh(report)
        if meter is None:
            return None
        mppt_fresh = REPORT_MPPT in report.updated
        charge = self.device.mppt_channels.charge if mppt_fresh else None
        charge_module = None if charge is None else self.device.mppt_module(charge)
        return self._load.update(
            meter_power_w=meter.w,
            inverter_power_w=assume_present(self.device.inverter).w,
            meter_location=self._meter_locations.get(self._primary_meter_unit_id),
            pv_power_w=self.device.pv_power_w if mppt_fresh else None,
            storage_charge_power_w=None if charge_module is None else charge_module.dcw,
            storage_present=self.device.storage is not None,
        )

    def _grid_status(self, report: UpdateReport) -> str | None:
        meter = self._primary_meter_fresh(report)
        if meter is None:
            return None
        return grid_status(assume_present(self.device.inverter).hz, meter.hz)


class FroniusWebCoordinator(DataUpdateCoordinator["WebData"]):
    """Polls the web API on its own, slower interval."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        web_control: FroniusWebControl,
        *,
        interval: timedelta,
    ) -> None:
        """Bind to the web control the poll delegates to."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{entry.title} web API",
            update_interval=interval,
        )
        self.web_control = web_control

    async def _async_update_data(self) -> WebData:
        try:
            return await self.web_control.async_refresh()
        except Exception as err:  # the client raises plain RuntimeError/requests errors
            raise UpdateFailed(f"Fronius web API refresh failed: {err}") from err
