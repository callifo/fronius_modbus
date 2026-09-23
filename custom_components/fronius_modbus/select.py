"""Select platform: every FroniusSelectDescription becomes a FroniusSelect."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import FroniusConfigEntry
from .entities import FroniusEntity, FroniusSelectDescription, select_descriptions

# One write at a time: the inverter answers requests one after the other.
PARALLEL_UPDATES = 1


class FroniusSelect(FroniusEntity, SelectEntity):
    """A select whose option is its description's value_fn."""

    entity_description: FroniusSelectDescription

    @property
    def current_option(self) -> str | None:
        """The description's current label for the current runtime."""
        return self.entity_description.value_fn(self._runtime)

    async def async_select_option(self, option: str) -> None:
        """Resolve the label to its code, write it, then request a refresh."""
        options_map = self.entity_description.options_map
        code = next((c for c, label in options_map.items() if label == option), None)
        if code is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_option",
                translation_placeholders={"option": option},
            )
        await self.async_run_write(
            lambda: self.entity_description.set_fn(self._runtime, code)
        )
        if self.entity_description.source == "modbus":
            await self._runtime.modbus.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FroniusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add every select the current runtime produces."""
    runtime = entry.runtime_data
    async_add_entities(
        FroniusSelect(runtime, entry, description)
        for description in select_descriptions(runtime)
    )
