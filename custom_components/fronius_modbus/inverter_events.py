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
SEVERITIES: dict[int, str] = {1: "error", 2: "warning", 3: "info"}
VIEWERS: dict[int, str] = {1: "customer", 2: "technician", 3: "service"}
# The event type of a severity in no known form: an info would slip past an
# automation that watches errors and warnings.
UNKNOWN_SEVERITY = "unknown"
EVENT_TYPES = (*SEVERITIES.values(), UNKNOWN_SEVERITY)
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


def _level(levels: Mapping[int, str], value: Any) -> str | None:
    # A list here raised as a dictionary key and failed the whole web poll.
    return levels.get(value) if isinstance(value, int) else None


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
        severity=_level(SEVERITIES, entry.get("severity")),
        visible_to=_level(VIEWERS, entry.get("viewer")),
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
    """Which log entries are new, held until a poll that publishes them.

    The first read only learns the log: its entries happened before the
    integration watched, and firing them at every restart would repeat them.
    When polls failed before it, the log alone cannot tell history from what
    started meanwhile; the entries that started after the first failure are new.
    """

    def __init__(self) -> None:
        """Start without having read the log."""
        self._seen: set[str] | None = None
        self._unread_since: float | None = None
        self.pending: tuple[InverterEvent, ...] = ()
        self.readable = False

    def read(self, log: tuple[InverterEvent, ...] | None, now: float) -> None:
        """Take a read of the log, None when it did not answer at `now`."""
        self.readable = log is not None
        if log is None:
            self.missed(now)
            return
        seen, self._seen = self._seen, {event.uuid for event in log}
        fresh = [event for event in log if self._is_new(event, seen)]
        by_uuid = {event.uuid: event for event in (*self.pending, *fresh)}
        self.pending = tuple(sorted(by_uuid.values(), key=lambda event: event.started))

    def _is_new(self, event: InverterEvent, seen: set[str] | None) -> bool:
        if seen is not None:
            return event.uuid not in seen
        # ponytail: the inverter's clock against Home Assistant's; a drift
        # between them moves this boundary, and only after a failed first read.
        # Take the inverter's own time instead should it ever serve one.
        return self._unread_since is not None and event.started >= self._unread_since

    def missed(self, now: float) -> None:
        """A poll failed at `now`; before the first read, what starts from now is new."""
        if self._seen is None and self._unread_since is None:
            self._unread_since = now

    @property
    def batch(self) -> tuple[InverterEvent, ...]:
        """The pending entries a poll may publish.

        None while the log does not answer: the event entity is unavailable
        then, and an entry fired into it would become no event state.
        """
        return self.pending if self.readable else ()

    def delivered(self, batch: tuple[InverterEvent, ...]) -> None:
        """A poll published `batch`; a failed one keeps its entries pending."""
        published = {event.uuid for event in batch}
        remaining = (event for event in self.pending if event.uuid not in published)
        self.pending = tuple(remaining)
