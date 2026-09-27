"""Sensor platform: every FroniusSensorDescription becomes a FroniusSensor."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import FroniusConfigEntry
from .entities import FroniusSensorDescription, sensor_descriptions
from .entity_base import FroniusEntity, FroniusTotalSensor

# The coordinator polls; entities never fetch on their own.
PARALLEL_UPDATES = 0

_TOTAL_STATE_CLASSES = (SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING)


class FroniusSensor(FroniusEntity, SensorEntity):
    """A sensor whose state is its description's value_fn."""

    entity_description: FroniusSensorDescription

    @classmethod
    def create(
        cls,
        runtime,
        entry: FroniusConfigEntry,
        description: FroniusSensorDescription,
    ) -> FroniusEntity:
        """Build a FroniusTotalSensor for total(-increasing) sensors, else a plain one."""
        if description.state_class in _TOTAL_STATE_CLASSES:
            return FroniusTotalSensor(runtime, entry, description)
        return cls(runtime, entry, description)

    async def async_added_to_hass(self) -> None:
        """Follow the web poll too where the description reads it besides Modbus."""
        await super().async_added_to_hass()
        web = self._runtime.web
        if self.entity_description.also_web and web is not None:
            self.async_on_remove(
                web.async_add_listener(self._handle_coordinator_update)
            )

    @property
    def native_value(self):
        """The description's value for the current runtime."""
        return self.entity_description.value_fn(self._runtime)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FroniusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add every sensor the current runtime produces."""
    runtime = entry.runtime_data
    async_add_entities(
        FroniusSensor.create(runtime, entry, description)
        for description in sensor_descriptions(runtime)
    )
