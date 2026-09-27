"""The inverter's event log: which events are active, which one leads, what is new.

The entries have the shape /api/status/activeEvents and /api/status/events
answered on a GEN24 on 2026-09-27; the uuids are made up.
"""

from custom_components.fronius_modbus.inverter_events import (
    EventTracker,
    event_texts,
    leading_event,
    parse_events,
)

TEXTS = event_texts(
    {
        "StateCodes": {
            "GEN24-1009": "Selbst­test der Licht­bo­gen-Er­ken­nung (AFCI) fehl­ge­schla­gen",
            "GEN24-1175": "Zu wenig DC-Leis­tung für Ein­spei­se­be­trieb",
        }
    }
)


def entry(uuid, prefix, event_id, label, severity, viewer, start, end=None):
    return {
        "activeUntil": end,
        "category": [0],
        "confirmable": False,
        "eventID": event_id,
        "label": label,
        "prefix": prefix,
        "severity": severity,
        "sourceID": 6,
        "subID": 0,
        "timestamp": start,
        "type": 0,
        "uuid": uuid,
        "viewer": viewer,
    }


AFCI = entry("a", "GEN24", 1009, "AfciSelftestFailed", 2, 3, 1788683992)
POWER_LOW = entry("b", "GEN24", 1175, "PowerLow", 2, 1, 1790470000)
NO_BATTERY_VOLTAGE = entry(
    "c", "GEN24", 1187, "NoBatteryVoltageMeasured", 1, 1, 1790000000
)
# The log's entries without a code carry neither a label nor a text.
PLACEHOLDER = entry("d", "IG24", 4294967295, "", 3, 3, 1788683900)


def test_an_event_reads_as_its_code_its_text_and_its_levels():
    (event,) = parse_events([AFCI], TEXTS)

    assert event.code == "GEN24-1009"
    assert event.text == "Selbsttest der Lichtbogen-Erkennung (AFCI) fehlgeschlagen"
    assert (event.severity, event.visible_to) == ("warning", "service")
    assert (event.started, event.ended, event.confirmable) == (1788683992, None, False)


def test_an_event_without_a_text_shows_its_label():
    (event,) = parse_events([NO_BATTERY_VOLTAGE], TEXTS)

    assert event.text == "NoBatteryVoltageMeasured"
    assert event.severity == "error"


def test_an_entry_without_a_code_is_no_event():
    assert parse_events([PLACEHOLDER, AFCI], TEXTS) == parse_events([AFCI], TEXTS)


def test_an_answer_that_is_no_list_is_no_reading():
    assert parse_events(None, TEXTS) is None
    assert parse_events({"error": "x"}, TEXTS) is None


def test_an_error_leads_a_warning():
    events = parse_events([POWER_LOW, NO_BATTERY_VOLTAGE], TEXTS)

    assert leading_event(events).code == "GEN24-1187"


def test_among_equal_levels_the_newest_leads():
    """Measured: at night PowerLow joins the AFCI warning and leads until morning."""
    events = parse_events([AFCI, POWER_LOW], TEXTS)

    assert leading_event(events).code == "GEN24-1175"


def test_no_active_event_leads_nothing():
    assert leading_event(()) is None


def test_the_first_read_of_the_log_only_learns_it():
    """Old entries fired at every restart would flood the logbook."""
    tracker = EventTracker()

    assert tracker.new(parse_events([AFCI, POWER_LOW], TEXTS)) == ()


def test_a_later_read_reports_only_what_is_new_oldest_first():
    """A zero-length entry (the battery's BYD2-44) is new like any other."""
    tracker = EventTracker()
    tracker.new(parse_events([AFCI], TEXTS))
    later = entry("e", "BYD2", 44, "", 2, 1, 1790500001, 1790500001)
    earlier = entry("f", "GEN24", 1187, "NoBatteryVoltageMeasured", 1, 1, 1790500000)

    new = tracker.new(parse_events([later, AFCI, earlier], TEXTS))

    assert [event.code for event in new] == ["GEN24-1187", "BYD2-44"]
    assert tracker.new(parse_events([later, AFCI, earlier], TEXTS)) == ()


def test_texts_that_are_no_mapping_are_no_texts():
    assert event_texts(None) == {}
    assert event_texts({"StateCodes": "x"}) == {}
