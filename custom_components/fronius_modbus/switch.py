"""Switch platform: every FroniusSwitchDescription becomes a FroniusSwitch."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import FroniusConfigEntry
from .entities import FroniusEntity, FroniusSwitchDescription, switch_descriptions

# One write at a time: the inverter answers requests one after the other.
PARALLEL_UPDATES = 1

# The solar-API switch is the one web write whose effect the entity must show
# immediately; every other web write relies on the control's own delayed refresh.
_IMMEDIATE_REFRESH_KEY = "api_solar_api_enabled"


class FroniusSwitch(FroniusEntity, SwitchEntity):
    """A switch whose state is its description's value_fn."""

    entity_description: FroniusSwitchDescription

    @property
    def is_on(self) -> bool | None:
        """The description's current state for the current runtime."""
        return self.entity_description.value_fn(self._runtime)

    async def async_turn_on(self, **kwargs) -> None:
        """Run the description's turn_on action."""
        await self._async_write(self.entity_description.turn_on)

    async def async_turn_off(self, **kwargs) -> None:
        """Run the description's turn_off action."""
        await self._async_write(self.entity_description.turn_off)

    async def _async_write(self, action) -> None:
        await self.async_run_write(lambda: action(self._runtime))
        if self.entity_description.source == "modbus":
            await self._runtime.modbus.async_request_refresh()
        elif self.entity_description.key == _IMMEDIATE_REFRESH_KEY:
            await self._runtime.web.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FroniusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add every switch the current runtime produces."""
    runtime = entry.runtime_data
    async_add_entities(
        FroniusSwitch(runtime, entry, description)
        for description in switch_descriptions(runtime)
    )
