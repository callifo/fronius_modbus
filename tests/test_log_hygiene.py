"""No log line names the device, and an expected outage is not logged as an error.

Home Assistant logs are the usual attachment to a bug report, so a serial, a
host name or a token in one of them travels further than the owner intends.
"""

import ast
from datetime import timedelta
import logging
import pathlib

from modbus_connection import ModbusConnectionError, ServerDeviceFailureError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
from .test_web_control import make_control

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "custom_components"

# Names that identify the installation rather than the problem. `unit_id` and
# the Modbus addresses stay allowed: they describe the protocol, not the owner.
DEVICE_IDENTIFIERS = frozenset({"host", "hostname", "serial", "token", "ip", "url"})


def _identifiers_in(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id.lower())
        elif isinstance(child, ast.Attribute):
            names.add(child.attr.lower().lstrip("_"))
    return names


def _logging_calls(tree: ast.AST):
    for node in ast.walk(tree):
        is_logger_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id.lower().endswith("logger")
        )
        if is_logger_call:
            yield node


def _offending_lines(source: pathlib.Path) -> list[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    offences = []
    for call in _logging_calls(tree):
        for argument in call.args[1:]:
            named = _identifiers_in(argument) & DEVICE_IDENTIFIERS
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
    """Audit E04: requests' HTTPError is an OSError, so a 500 read as an outage."""
    await _web_refresh_failing_with(hass, FroniusWebResponseError("HTTP 500"), caplog)
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
    """Reaudit R02: a second call site bypassed the boundary that strips the host."""
    offences = [
        f"{source.name}:{line}"
        for source in sorted(PACKAGE.rglob("*.py"))
        for line in _requests_calls_outside_the_boundary(
            ast.parse(source.read_text(encoding="utf-8"))
        )
    ]
    assert offences == []
