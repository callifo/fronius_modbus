"""The base entity every platform builds, and the devices entities belong to."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import cast

from modbus_connection import ModbusError

from homeassistant.components.sensor import RestoreSensor
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, entity_prefix, instance_key
from .coordinator import (
    FroniusConfigEntry,
    FroniusModbusCoordinator,
    FroniusRuntimeData,
    FroniusWebCoordinator,
    assume_present,
)
from .derived import TotalGuard
from .descriptions import DeviceKind, FroniusDescription, WebClientKind
from .fronius_modbus_api.exceptions import ControlRefused, ControlUnavailable
from .froniuswebclient import FroniusWebUnreachable

_LOGGER = logging.getLogger(__name__)

# A new poll below the last value, or a jump above it, is a bad reading, not a reset.
TOTAL_INCREASING_MAX_STEP_WH = 100_000
# ... unless the lower value keeps coming: a meter or inverter swap really does
# restart the counter, and refusing it forever would freeze the sensor for good.
TOTAL_INCREASING_RESET_POLLS = 3


def _web_client_present(runtime: FroniusRuntimeData, kind: WebClientKind) -> bool:
    control = runtime.web_control
    if control is None or kind == "public":
        return control is not None
    return control.technician_configured if kind == "technician" else control.configured


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
        readings = (web_data.storage_readings if web_data else None) or {}
        return DeviceInfo(
            identifiers={(DOMAIN, f"{key}_battery_storage")},
            name=storage_model or "Battery Storage",
            manufacturer=web_data.storage_manufacturer if web_data else None,
            model=storage_model,
            serial_number=web_data.storage_serial if web_data else None,
            sw_version=readings.get("sw_version"),
            hw_version=readings.get("hw_version"),
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


def expected_device_identifiers(
    runtime: FroniusRuntimeData, entry: FroniusConfigEntry
) -> set[str]:
    """The device identifiers the current runtime registers, in the shape device_info builds."""
    key = instance_key(entry.entry_id)
    identifiers = {f"{key}_inverter"}
    if runtime.device.storage is not None:
        identifiers.add(f"{key}_battery_storage")
    identifiers.update(f"{key}_meter_{unit_id}" for unit_id in runtime.device.meters)
    return identifiers


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
    def _source_refreshed(self) -> bool:
        """Whether the last poll actually refreshed the report this entity reads.

        The one owner of that question: `available` hides a stale entity, and a
        total sensor must not take a stale cached value for a fresh sample
        (audit A02).
        """
        # Home Assistant types the attribute as a plain EntityDescription, and
        # a narrower annotation here collides with the platform base classes.
        description = cast(FroniusDescription, self.entity_description)
        if description.source == "modbus":
            coordinator = self._runtime.modbus
            if not coordinator.last_update_success:
                web = self._runtime.web
                return description.also_web and bool(web and web.last_update_success)
            return (
                description.report_name is None
                or description.report_name in coordinator.data.report.updated
            )
        # A rejected login clears the client but the coordinator keeps
        # succeeding on what is left (audit F16): the entities of that login
        # must not stay operable.
        if not _web_client_present(self._runtime, description.web_client):
            return False
        web_coordinator = self._runtime.web
        return web_coordinator is not None and web_coordinator.last_update_success

    @property
    def _source_sampled(self) -> bool:
        """Whether this poll read the source, rather than re-serving the last one.

        Availability is happy with retained data - that is what the tolerance
        window around a web write is for - but a value the device did not send
        again is no evidence about the device (audit B05).
        """
        if not self._source_refreshed:
            return False
        description = cast(FroniusDescription, self.entity_description)
        if description.source != "modbus":
            return True
        return not self._runtime.modbus.data.retained

    @property
    def available(self) -> bool:
        """Whether the report backing this entity was refreshed, and available_fn agrees."""
        if not self._source_refreshed:
            return False
        description = cast(FroniusDescription, self.entity_description)
        return description.available_fn(self._runtime)

    async def async_run_write(self, action: Callable[[], Awaitable[None]]) -> None:
        """Await a write action, mapping its errors to the ones HA expects."""
        try:
            await action()
        except ControlRefused as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=err.key,
                translation_placeholders=err.placeholders,
            ) from err
        except ControlUnavailable as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=err.key,
                translation_placeholders=err.placeholders,
            ) from err
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="value_refused",
                translation_placeholders={"error": str(err)},
            ) from err
        except FroniusWebUnreachable as err:
            # An OSError, so the switched-off device reads like a Modbus
            # outage everywhere else; no write mapped it (audit R24-02).
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="web_api_unreachable",
            ) from err
        except (ModbusError, RuntimeError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"error": str(err)},
            ) from err


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
        # A failed read leaves the component holding what it decoded last, so
        # feeding it again would let one bad reading confirm itself over two
        # failed polls (audit A02). Only a refreshed source is an observation.
        reading = description.value_fn(self._runtime) if self._source_sampled else None
        verdict = self._guard.observe(reading)
        if verdict is None:
            return
        # An ignored reading changes nothing; an adopted jump moves the statistics.
        level = logging.WARNING if verdict.adopted else logging.DEBUG
        _LOGGER.log(level, "%s: %s", self.entity_id, verdict.message)

    @property
    def available(self) -> bool:
        """A total sensor always shows its last accepted value."""
        return True

    @property
    def native_value(self) -> float | None:
        """The last value the guard accepted; reading it changes nothing."""
        return self._guard.value
