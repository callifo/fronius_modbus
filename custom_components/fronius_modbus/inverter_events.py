"""The inverter's event log, as its web interface lists it.

Modbus carries events only as severity bits (model 103 EvtVnd1/2), and none
for the service view; the codes, their time and their text come from the
web interface's own endpoints, which answer without a login.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ACTIVE_EVENTS_PATH = "/api/status/activeEvents"
EVENT_LOG_PATH = "/api/status/events"
# The texts the web interface shows for a code, per language.
EVENT_TEXTS_PATH = "/app/assets/i18n/StateCodeTranslations/{language}.json"
EVENT_TEXTS_FALLBACK_LANGUAGE = "en"
_TEXTS_SECTION = "StateCodes"
_SOFT_HYPHEN = "­"
# Read from the log of a GEN24: PowerLow is a warning, the daily isolation
# measurement an info; the service view holds what Modbus never reports.
SEVERITIES = {1: "error", 2: "warning", 3: "info"}
VIEWERS = {1: "customer", 2: "technician", 3: "service"}
# Log entries of this id carry neither a label nor a text.
_NO_CODE = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class InverterEvent:
    """One entry of the inverter's event list."""

    uuid: str
    code: str
    label: str
    text: str
    severity: str | None
    visible_to: str | None
    started: int
    ended: int | None
    confirmable: bool


def event_texts(payload: Any) -> dict[str, str]:
    """The text per code from a StateCodeTranslations file; none from anything else."""
    section = payload.get(_TEXTS_SECTION) if isinstance(payload, Mapping) else None
    if not isinstance(section, Mapping):
        return {}
    return {
        str(code): text.replace(_SOFT_HYPHEN, "")
        for code, text in section.items()
        if isinstance(text, str)
    }


def _event(entry: Mapping[str, Any], texts: Mapping[str, str]) -> InverterEvent | None:
    event_id = entry.get("eventID")
    started = entry.get("timestamp")
    if not isinstance(event_id, int) or event_id == _NO_CODE:
        return None
    if not isinstance(started, int):
        return None
    code = f"{entry.get('prefix')}-{event_id}"
    label = str(entry.get("label") or "")
    ended = entry.get("activeUntil")
    return InverterEvent(
        uuid=str(entry.get("uuid")),
        code=code,
        label=label,
        text=texts.get(code) or label or code,
        severity=SEVERITIES.get(entry.get("severity")),
        visible_to=VIEWERS.get(entry.get("viewer")),
        started=started,
        ended=ended if isinstance(ended, int) else None,
        confirmable=entry.get("confirmable") is True,
    )


def parse_events(
    payload: Any, texts: Mapping[str, str]
) -> tuple[InverterEvent, ...] | None:
    """The events of an answer; None when the answer is no list."""
    if not isinstance(payload, list):
        return None
    events = (_event(entry, texts) for entry in payload if isinstance(entry, Mapping))
    return tuple(event for event in events if event is not None)


def _rank(event: InverterEvent) -> tuple[int, int]:
    order = list(SEVERITIES.values())
    level = order.index(event.severity) if event.severity in order else len(order)
    return (level, -event.started)


def leading_event(events: Iterable[InverterEvent]) -> InverterEvent | None:
    """The most severe active event, the newest among equals."""
    return min(events, key=_rank, default=None)


class EventTracker:
    """Which log entries are new since the last read.

    The first read only learns the log: its entries happened before the
    integration watched, and firing them at every restart would repeat them.
    """

    def __init__(self) -> None:
        """Start without having read the log."""
        self._seen: set[str] | None = None

    def new(self, log: Iterable[InverterEvent]) -> tuple[InverterEvent, ...]:
        """The entries not seen before, oldest first."""
        entries = list(log)
        seen, self._seen = self._seen, {event.uuid for event in entries}
        if seen is None:
            return ()
        fresh = (event for event in entries if event.uuid not in seen)
        return tuple(sorted(fresh, key=lambda event: event.started))
