"""How the inverter's events read as entity states and attributes."""

from __future__ import annotations

from typing import Any, Literal

from homeassistant.util import dt as dt_util

from .coordinator import FroniusRuntimeData
from .inverter_events import CUSTOMER_LEVEL, InverterEvent, leading_event

# Whom an event entity serves: the customer sees what the web interface shows
# a customer login, whatever the entry's own login (#39); the service
# entities, off by default, get the rest.
type EventAudience = Literal["customer", "service"]
CUSTOMER: EventAudience = "customer"
SERVICE: EventAudience = "service"
# The key part that tells the service entities apart; the customer's keep theirs.
EVENT_AUDIENCES: tuple[tuple[EventAudience, str], ...] = (
    (CUSTOMER, ""),
    (SERVICE, "service_"),
)

# The state while no event is active: event texts come from the inverter in
# the user's language, so the one fixed state is one every language reads.
NO_ACTIVE_EVENT = "OK"


def for_audience(event: InverterEvent, audience: EventAudience) -> bool:
    """Whether the event belongs to the audience's entities."""
    return (event.visible_to == CUSTOMER_LEVEL) == (audience == CUSTOMER)


def _active_events(
    runtime: FroniusRuntimeData, audience: EventAudience
) -> tuple[InverterEvent, ...] | None:
    web_data = runtime.web_data
    if web_data is None or web_data.active_events is None:
        return None
    return tuple(
        event for event in web_data.active_events if for_audience(event, audience)
    )


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


def leading_event_text(
    runtime: FroniusRuntimeData, audience: EventAudience
) -> str | None:
    """The text of the leading active event; unknown while the list is unread."""
    events = _active_events(runtime, audience)
    if events is None:
        return None
    event = leading_event(events)
    return NO_ACTIVE_EVENT if event is None else event.text


def leading_event_attributes(
    runtime: FroniusRuntimeData, audience: EventAudience
) -> dict[str, Any] | None:
    """The leading event's details and the other active events."""
    events = _active_events(runtime, audience) or ()
    event = leading_event(events)
    if event is None:
        return None
    others = [event_attributes(other) for other in events if other is not event]
    return {**event_attributes(event), "other_events": others}


def active_event_count(
    runtime: FroniusRuntimeData, audience: EventAudience
) -> int | None:
    """How many events are active; unknown while the list is unread."""
    events = _active_events(runtime, audience)
    return None if events is None else len(events)
