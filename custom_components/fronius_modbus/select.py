"""Select platform: every FroniusSelectDescription becomes a FroniusSelect."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity

from .const import DOMAIN
from .coordinator import FroniusConfigEntry
from .entities import FroniusSelectDescription, select_descriptions
from .entity import FroniusEntity

# One write at a time: the inverter answers requests one after the other.
PARALLEL_UPDATES = 1


class FroniusSelect(FroniusEntity, SelectEntity, RestoreEntity):
    """A select whose option is its description's value_fn."""

    entity_description: FroniusSelectDescription

    async def async_added_to_hass(self) -> None:
        """Hand what the select kept before a restart to the description's restore_fn."""
        await super().async_added_to_hass()
        restore_fn = self.entity_description.restore_fn
        last = await self.async_get_last_extra_data()
        if restore_fn is None or last is None:
            return
        # Entities that depend on the option and started first follow with the
        # next poll: notifying them now handed the energy totals one poll twice.
        restore_fn(self._runtime, last.as_dict())

    @property
    def extra_restore_state_data(self) -> RestoredExtraData | None:
        """What the description keeps across a restart, if anything."""
        saved_fn = self.entity_description.saved_fn
        saved = None if saved_fn is None else saved_fn(self._runtime)
        return None if saved is None else RestoredExtraData(saved)

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
