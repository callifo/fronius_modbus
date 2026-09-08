"""Shared fixtures: the captured inverter replayed into the in-memory Modbus backend."""

import json
import pathlib

from modbus_connection.mock import MockModbusConnection
import pytest

from .durations import SLOW_TEST_SECONDS, over_budget

pytest_plugins = ("pytest_homeassistant_custom_component",)

# Summed per test across setup, call and teardown, and read at the end of the
# session. A dict at module level because that is what a pytest hook has: the
# hooks are functions, not a fixture with somewhere to keep state.
_durations: dict = {}


def pytest_addoption(parser):
    parser.addoption(
        "--slow-test-seconds",
        type=float,
        default=SLOW_TEST_SECONDS,
        help=(
            "fail the session if a single test takes longer than this "
            "(0 makes every test late, which is how the check is tested)"
        ),
    )


def pytest_runtest_logreport(report):
    """Add up what one test costs, fixtures included."""
    _durations[report.nodeid] = _durations.get(report.nodeid, 0.0) + report.duration


def pytest_sessionfinish(session, exitstatus):
    """Turn a green run red when a test ran far longer than it should.

    Only a green one: a failing suite has more urgent news, and a test that is
    slow *because* it failed is not the subject here.
    """
    if exitstatus != pytest.ExitCode.OK:
        return

    late = over_budget(_durations, session.config.getoption("--slow-test-seconds"))
    if not late:
        return

    listed = "\n  ".join(f"{seconds:7.2f}s {node_id}" for node_id, seconds in late)
    print(
        f"\nSLOWER THAN THE BUDGET ALLOWS:\n  {listed}\n\n"
        "A test in the minutes is nearly always a wait that was meant to be "
        "shortened and no longer is - check what the test patches against "
        "where the production code now reads it. If the time is genuinely "
        "warranted, raise SLOW_TEST_SECONDS in tests/durations.py and say "
        "in the commit why."
    )
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


FIXTURES = pathlib.Path(__file__).with_name("fixtures")
INVERTER_UNIT_ID = 1
METER_UNIT_ID = 200


def load_fixture(name: str) -> dict[int, dict[str, dict[int, int]]]:
    """A captured device as {unit_id: {"holding": {address: word}}}."""
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return {
        int(unit_id): {
            space: {int(a): v for a, v in words.items()}
            for space, words in spaces.items()
        }
        for unit_id, spaces in raw.items()
    }


@pytest.fixture
def symo_gen24() -> dict:
    return load_fixture("symo_gen24_fw1386.json")


@pytest.fixture
def connection(symo_gen24) -> MockModbusConnection:
    """A Symo GEN24 10.0 with one Smart Meter on unit 200, in memory."""
    connection = MockModbusConnection()
    for unit_id, spaces in symo_gen24.items():
        connection.for_unit(unit_id).load_raw(spaces)
    return connection


@pytest.fixture
def inverter_unit(connection):
    return connection.for_unit(INVERTER_UNIT_ID)


@pytest.fixture
def meter_unit(connection):
    return connection.for_unit(METER_UNIT_ID)


CORE_CONNECTION = "homeassistant.components.modbus.connection.ModbusConnection"


@pytest.fixture(autouse=True)
def _no_real_inverter(monkeypatch):
    """Fail loudly instead of dialling out when a test forgets the mock_modbus fixture."""

    def _refuse(params, **_kwargs):
        raise AssertionError(
            "a test reached a real Modbus device - use the mock_modbus fixture"
        )

    monkeypatch.setattr(CORE_CONNECTION, _refuse)


class SharedMockModbus:
    """Stands in for the core modbus integration's connection factory, seeding every unit from the fixture."""

    def __init__(self, fixture: dict) -> None:
        self.params_seen: list = []
        self.connections: list[MockModbusConnection] = []
        self._fixture = fixture
        self._request_failures: dict[int, Exception | None] = {}
        self._read_failures: dict[tuple[int, int], Exception | None] = {}

    def __call__(self, params, **_kwargs) -> MockModbusConnection:
        self.params_seen.append(params)
        connection = MockModbusConnection()
        for unit_id, spaces in self._fixture.items():
            connection.for_unit(unit_id).load_raw(spaces)
        for unit_id, error in self._request_failures.items():
            connection.for_unit(unit_id).fail_requests(error)
        for (unit_id, address), error in self._read_failures.items():
            connection.for_unit(unit_id).fail_read(address, error)
        self.connections.append(connection)
        return connection

    def add_unit(self, unit_id: int, like: int) -> None:
        """Serve another unit id from an existing unit's captured registers."""
        self._fixture[unit_id] = self._fixture[like]

    def unit(self, unit_id: int):
        """The unit on the connection the integration is currently holding."""
        return self.connections[-1].for_unit(unit_id)

    @property
    def connected(self) -> bool:
        """Whether the current connection is up."""
        return bool(self.connections) and self.connections[-1].connected

    def fail_read(self, unit_id: int, address: int, error: Exception | None) -> None:
        """Make one register block unreadable, on the current connection and every later one."""
        self._read_failures[(unit_id, address)] = error
        for connection in self.connections:
            connection.for_unit(unit_id).fail_read(address, error)

    def fail_requests(self, unit_id: int, error: Exception | None) -> None:
        """Make a unit stop answering, on the current connection and every later one."""
        self._request_failures[unit_id] = error
        for connection in self.connections:
            connection.for_unit(unit_id).fail_requests(error)


@pytest.fixture
def mock_modbus(monkeypatch, symo_gen24):
    shared = SharedMockModbus(symo_gen24)
    monkeypatch.setattr(CORE_CONNECTION, shared)
    return shared
