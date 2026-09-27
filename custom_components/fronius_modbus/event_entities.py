"""How the inverter's events read as entity states and attributes."""

from __future__ import annotations

from typing import Any

from homeassistant.util import dt as dt_util

from .coordinator import FroniusRuntimeData
from .inverter_events import InverterEvent, leading_event

# The state while no event is active: event texts come from the inverter in
# the user's language, so the one fixed state is one every language reads.
NO_ACTIVE_EVENT = "OK"


def _active_events(runtime: FroniusRuntimeData) -> tuple[InverterEvent, ...] | None:
    web_data = runtime.web_data
    return None if web_data is None else web_data.active_events


def event_attributes(event: InverterEvent) -> dict[str, Any]:
    """What an event entity or the leading-event sensor tells about one event."""
    return {
        "code": event.code,
        "text": event.text,
        "severity": event.severity,
        "visible_to": event.visible_to,
        "since": dt_util.utc_from_timestamp(event.started),
        "confirmable": event.confirmable,
    }


def leading_event_text(runtime: FroniusRuntimeData) -> str | None:
    """The text of the leading active event; unknown while the list is unread."""
    events = _active_events(runtime)
    if events is None:
        return None
    event = leading_event(events)
    return NO_ACTIVE_EVENT if event is None else event.text


def leading_event_attributes(runtime: FroniusRuntimeData) -> dict[str, Any] | None:
    """The leading event's details and the other active events."""
    events = _active_events(runtime) or ()
    event = leading_event(events)
    if event is None:
        return None
    others = [event_attributes(other) for other in events if other is not event]
    return {**event_attributes(event), "other_events": others}


def active_event_count(runtime: FroniusRuntimeData) -> int | None:
    """How many events are active; unknown while the list is unread."""
    events = _active_events(runtime)
    return None if events is None else len(events)
