"""No log line names the device, and an expected outage is not logged as an error.

Home Assistant logs are the usual attachment to a bug report, so a serial, a
host name or a token in one of them travels further than the owner intends.
"""

import ast
from datetime import timedelta
import logging
import pathlib

from modbus_connection import (
    GatewayTargetError,
    ModbusConnectionError,
    ServerDeviceFailureError,
)
import pytest

from custom_components.fronius_modbus.const import DOMAIN
from custom_components.fronius_modbus.coordinator import (
    FroniusModbusCoordinator,
    FroniusWebCoordinator,
)
from custom_components.fronius_modbus.fronius_modbus_api.device import FroniusInverter
from custom_components.fronius_modbus.froniuswebclient import (
    FroniusWebResponseError,
    FroniusWebUnreachable,
)

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID
from .ha_test_plugin import MockConfigEntry
from .test_web_control import make_control

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "custom_components"

# Names that identify the installation rather than the problem. `unit_id` and
# the Modbus addresses stay allowed: they describe the protocol, not the owner.
# The entry title holds the host by default, and so did unique ids of 0.3.x.
DEVICE_IDENTIFIERS = frozenset(
    {
        "host",
        "hostname",
        "conf_host",
        "serial",
        "serial_number",
        "token",
        "password",
        "ip",
        "url",
        "title",
        "unique_id",
    }
)
# A part of a longer name counts too: allowed_host, local_ip and entry_title
# carry the same value under another name.
NAME_PARTS = frozenset(
    {"host", "hostname", "serial", "token", "password", "ip", "url", "title"}
)
# Objects that carry the host or serial among their fields. A field read from
# one counts only for the dicts that hold the settings: entry.entry_id and
# identity.firmware name no installation.
WHOLE_OBJECTS = frozenset(
    {"entry", "entries", "config_entry", "config_entries", "identity", "identities"}
)
SETTINGS_FIELDS = frozenset({"data", "options"})
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log"}
)


def _identifiers_in(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id.lower())
        elif isinstance(child, ast.Attribute):
            names.add(child.attr.lower().lstrip("_"))
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            # entry.data["host"] and {"host": ...} name it as a string.
            names.add(child.value.lower())
    return names


def _is_logger(receiver: ast.AST) -> bool:
    """_LOGGER, LOG, self._logger: the usual names of a logger object."""
    name = None
    if isinstance(receiver, ast.Name):
        name = receiver.id
    elif isinstance(receiver, ast.Attribute):
        name = receiver.attr
    if name is None:
        return False
    name = name.lower().lstrip("_")
    return name == "log" or name.endswith("logger")


def _logging_calls(tree: ast.AST):
    for node in ast.walk(tree):
        is_logger_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in LOG_METHODS
            and _is_logger(node.func.value)
        )
        if is_logger_call:
            yield node


def _names_the_installation(name: str) -> bool:
    # hosts and allowed_ips carry the same values as host and ip.
    parts = {part.removesuffix("s") for part in name.split("_")}
    return name in DEVICE_IDENTIFIERS or bool(NAME_PARTS & parts)


def _object_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id.lower().lstrip("_")
    if isinstance(node, ast.Attribute):
        return node.attr.lower().lstrip("_")
    return None


def _whole_objects(argument: ast.AST) -> set[str]:
    """Entry and identity objects in the argument, unless only a field is read."""
    field_read = {
        id(node.value): node.attr
        for node in ast.walk(argument)
        if isinstance(node, ast.Attribute)
    }
    named = set()
    for node in ast.walk(argument):
        name = _object_name(node)
        if name not in WHOLE_OBJECTS:
            continue
        field = field_read.get(id(node))
        if field is None or field in SETTINGS_FIELDS:
            named.add(name)
    return named


def _offending_lines(source: pathlib.Path) -> list[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    offences = []
    for call in _logging_calls(tree):
        arguments = [*call.args[1:], *(keyword.value for keyword in call.keywords)]
        for argument in arguments:
            named = {
                name
                for name in _identifiers_in(argument)
                if _names_the_installation(name)
            } | _whole_objects(argument)
            if named:
                offences.append(f"{source.name}:{call.lineno} passes {sorted(named)}")
    return offences


def test_no_log_line_names_the_device():
    """A device identifier belongs in diagnostics, redacted - not in the log."""
    offences = [
        offence
        for source in sorted(PACKAGE.rglob("*.py"))
        for offence in _offending_lines(source)
    ]

    assert not offences, (
        f"log call(s) naming the installation: {offences}. Drop the value from "
        "the message: the log already says which integration wrote it, and "
        "diagnostics carry the host and serial redacted."
    )


def test_the_scan_catches_the_shape_it_was_written_for(tmp_path):
    written_by_hand = tmp_path / "sample.py"
    written_by_hand.write_text(
        '_LOGGER.warning("cannot reach %s: %s", self._host, err)\n', encoding="utf-8"
    )

    assert _offending_lines(written_by_hand)


@pytest.mark.parametrize(
    "line",
    [
        '_LOGGER.info("set up %s", entry.title)',
        '_LOGGER.info("set up %s", entry.data[CONF_HOST])',
        '_LOGGER.info("set up %s", entry.data["host"])',
        '_LOGGER.info("found %s", identity.serial_number)',
        '_LOGGER.info("entry %s", entry.unique_id)',
        '_LOGGER.debug("login %s", self._password)',
        '_LOGGER.warning("failed", extra={"host": host})',
        'self._logger.info("cannot reach %s", host)',
        'LOG.info("cannot reach %s", host)',
        '_LOGGER.info("allowing %s", allowed_host)',
        '_LOGGER.info("allowing %s", self.data.modbus_restriction_ip)',
        '_LOGGER.info("found %s", local_ip)',
        '_LOGGER.info("set up %s", entry_title(entry.data))',
        '_LOGGER.info("allowed %s", hosts)',
        '_LOGGER.info("found %s", allowed_ips)',
        '_LOGGER.info("set up %s", entry)',
        '_LOGGER.info("found %s", self._entry)',
        '_LOGGER.info("found %s", identity)',
        '_LOGGER.info("set up %s", str(entry))',
        '_LOGGER.info("set up %s", [entry])',
        '_LOGGER.info("set up %s", entries)',
        '_LOGGER.info("set up %s", entry.data)',
        '_LOGGER.info("set up %s", self.config_entry.data)',
        '_LOGGER.info("found %s", identities)',
    ],
)
def test_the_scan_catches_the_other_usual_ways_to_name_the_device(tmp_path, line):
    """Only a name or attribute spelled exactly like one of six words was caught."""
    written_by_hand = tmp_path / "sample.py"
    written_by_hand.write_text(f"{line}\n", encoding="utf-8")

    assert _offending_lines(written_by_hand)


@pytest.mark.parametrize(
    "line",
    [
        '_LOGGER.info("set up %s", entry.entry_id)',
        '_LOGGER.info("set up %s", self._entry.state)',
        '_LOGGER.info("found %d", len(identity.firmware))',
    ],
)
def test_the_scan_lets_a_field_that_names_no_installation_through(tmp_path, line):
    """The counter-check: an entry id or state identifies no installation."""
    written_by_hand = tmp_path / "sample.py"
    written_by_hand.write_text(f"{line}\n", encoding="utf-8")

    assert not _offending_lines(written_by_hand)


async def test_an_unreachable_device_is_not_logged_as_an_error(
    coordinator, inverter_unit, caplog
):
    """The quality scale asks for info when a device goes away; the coordinator uses error."""
    inverter_unit.fail_requests(ModbusConnectionError())

    with caplog.at_level(logging.DEBUG):
        await coordinator.async_refresh()

    fetch_failures = [
        record for record in caplog.records if record.msg.startswith("Error fetching")
    ]
    assert fetch_failures
    assert [record.levelno for record in fetch_failures] == [logging.INFO]


async def test_a_device_that_answers_and_refuses_is_still_an_error(
    coordinator, inverter_unit, caplog
):
    """The counter-check: without it the filter would quietly downgrade everything."""
    inverter_unit.fail_requests(ServerDeviceFailureError())

    with caplog.at_level(logging.DEBUG):
        await coordinator.async_refresh()

    fetch_failures = [
        record for record in caplog.records if record.msg.startswith("Error fetching")
    ]
    assert fetch_failures
    assert [record.levelno for record in fetch_failures] == [logging.ERROR]


@pytest.fixture
def coordinator(hass, connection):
    """A Modbus coordinator on the captured device."""
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.0.2.1"})
    entry.add_to_hass(hass)
    device = FroniusInverter(
        connection.for_unit(INVERTER_UNIT_ID),
        INVERTER_UNIT_ID,
        {METER_UNIT_ID: connection.for_unit(METER_UNIT_ID)},
    )
    return FroniusModbusCoordinator(
        hass,
        entry,
        device,
        interval=timedelta(seconds=10),
        primary_meter_unit_id=METER_UNIT_ID,
        meter_locations={METER_UNIT_ID: 0},
    )


def _fetch_failure_levels(caplog) -> list[int]:
    return [
        record.levelno
        for record in caplog.records
        if record.msg.startswith("Error fetching")
    ]


async def _web_refresh_failing_with(hass, error: Exception, caplog) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.0.2.1"})
    entry.add_to_hass(hass)
    control = make_control(hass)
    control._client.get_inverter_info = lambda: (_ for _ in ()).throw(error)
    coordinator = FroniusWebCoordinator(
        hass, entry, control, interval=timedelta(seconds=60)
    )
    try:
        with caplog.at_level(logging.DEBUG):
            await coordinator.async_refresh()
    finally:
        control.shutdown()


async def test_an_unreachable_web_api_is_not_logged_as_an_error(hass, caplog):
    await _web_refresh_failing_with(
        hass, FroniusWebUnreachable("ConnectionError"), caplog
    )
    assert _fetch_failure_levels(caplog) == [logging.INFO]


async def test_a_web_server_error_is_still_an_error(hass, caplog):
    """Requests' HTTPError is an OSError, so a 500 read as an outage."""
    await _web_refresh_failing_with(
        hass, FroniusWebResponseError("HTTP 500", 500), caplog
    )
    assert _fetch_failure_levels(caplog) == [logging.ERROR]


# The one function allowed to call into requests: its errors carry the URL.
REQUESTS_BOUNDARY = "_http"


def _requests_calls_outside_the_boundary(tree: ast.Module) -> list[int]:
    offences = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name == REQUESTS_BOUNDARY:
            continue
        for call in ast.walk(node):
            is_requests_call = (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "requests"
            )
            if is_requests_call:
                offences.append(call.lineno)
    return offences


def test_requests_is_called_in_one_place():
    """A second call site bypassed the boundary that strips the host."""
    offences = [
        f"{source.name}:{line}"
        for source in sorted(PACKAGE.rglob("*.py"))
        for line in _requests_calls_outside_the_boundary(
            ast.parse(source.read_text(encoding="utf-8"))
        )
    ]
    assert offences == []


def _meter_lines(caplog) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if "meter_200" in record.getMessage()
    ]


async def test_a_meter_that_stops_answering_is_logged_once_at_info_and_on_return(
    coordinator, connection, caplog
):
    """log-when-unavailable, per sub-system: a meter behind the inverter goes quiet
    now and then (the inverter reports gateway target failed); that is no fault the
    owner can fix, and the return deserves a line as much as the loss."""
    await coordinator.async_refresh()
    meter = connection.for_unit(METER_UNIT_ID)
    with caplog.at_level(logging.DEBUG):
        meter.fail_requests(GatewayTargetError())
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        meter.fail_requests(None)
        await coordinator.async_refresh()
        await coordinator.async_refresh()

    lines = _meter_lines(caplog)
    assert [level for level, _ in lines] == [logging.INFO, logging.INFO], lines
    assert "answers again" in lines[-1][1]


async def test_a_meter_that_answers_and_refuses_is_still_a_warning(
    coordinator, connection, caplog
):
    """The counter-check: a refusal is a device telling us something is wrong."""
    await coordinator.async_refresh()
    with caplog.at_level(logging.DEBUG):
        connection.for_unit(METER_UNIT_ID).fail_requests(ServerDeviceFailureError())
        await coordinator.async_refresh()

    assert [level for level, _ in _meter_lines(caplog)] == [logging.WARNING]
    assert "could not be read" in _meter_lines(caplog)[0][1]
