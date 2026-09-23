"""Button platform: every FroniusButtonDescription becomes a FroniusButton."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import FroniusConfigEntry
from .entities import FroniusButtonDescription, FroniusEntity, button_descriptions

# One write at a time: the inverter answers requests one after the other.
PARALLEL_UPDATES = 1


class FroniusButton(FroniusEntity, ButtonEntity):
    """A button that runs its description's press action."""

    entity_description: FroniusButtonDescription

    async def async_press(self) -> None:
        """Run the description's press action, then request a refresh on Modbus writes."""
        await self.async_run_write(lambda: self.entity_description.press(self._runtime))
        if self.entity_description.source == "modbus":
            await self._runtime.modbus.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FroniusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add every button the current runtime produces."""
    runtime = entry.runtime_data
    async_add_entities(
        FroniusButton(runtime, entry, description)
        for description in button_descriptions(runtime)
    )
