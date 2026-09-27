"""Event platform: the inverter's new log entries, one Home Assistant event each."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import FroniusConfigEntry, FroniusRuntimeData
from .entities import FroniusEventDescription, event_descriptions
from .entity_base import FroniusEntity
from .event_entities import event_attributes
from .inverter_events import UNCLASSIFIED_SEVERITY

# The coordinator polls; entities never fetch on their own.
PARALLEL_UPDATES = 0


class FroniusEvent(FroniusEntity, EventEntity):
    """Fires once for every entry the web poll found new in the inverter's log."""

    entity_description: FroniusEventDescription

    def __init__(
        self,
        runtime: FroniusRuntimeData,
        entry: FroniusConfigEntry,
        description: FroniusEventDescription,
    ) -> None:
        """Bind to the web coordinator; nothing fired yet."""
        super().__init__(runtime, entry, description)
        self._fired: set[str] = set()

    @property
    def available(self) -> bool:
        """Only while the log answers: otherwise nothing could ever fire."""
        web_data = self._runtime.web_data
        readable = web_data is not None and web_data.event_log_readable
        return readable and super().available

    @callback
    def _handle_coordinator_update(self) -> None:
        web_data = self._runtime.web_data
        batch = web_data.new_events if web_data is not None else ()
        # event.received ignores a change away from unavailable, so the event
        # comes back before it fires: fired together, the entry ran nothing.
        if batch:
            self.async_write_ha_state()
        # The same poll can reach the listeners twice (a write pushes the data
        # it holds again); an entry fires once.
        for event in batch:
            if event.uuid in self._fired:
                continue
            self._trigger_event(
                event.severity or UNCLASSIFIED_SEVERITY, event_attributes(event)
            )
            self.async_write_ha_state()
        self._fired = {event.uuid for event in batch}
        super()._handle_coordinator_update()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FroniusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the event entity the current runtime produces."""
    runtime = entry.runtime_data
    async_add_entities(
        FroniusEvent(runtime, entry, description)
        for description in event_descriptions(runtime)
    )
