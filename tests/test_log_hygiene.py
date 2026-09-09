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
from custom_components.fronius_modbus.coordinator import FroniusModbusCoordinator
from custom_components.fronius_modbus.fronius_modbus_api.device import FroniusInverter

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID

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
