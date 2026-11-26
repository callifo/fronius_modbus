"""Switching a Datamanager from the float models to int + SF, during the setup only."""

from typing import Any

from modbus_connection import IllegalDataValueError, ModbusTimeoutError
import pytest

from custom_components import fronius_modbus
from custom_components.fronius_modbus import datamanager, inverter_choice
from custom_components.fronius_modbus.const import CONF_INVERTER_UNIT_ID, DOMAIN
from custom_components.fronius_modbus.datamanager import DeviceFamily
from custom_components.fronius_modbus.fronius_modbus_api.device import FroniusInverter
from homeassistant.data_entry_flow import FlowResultType

from .conftest import CORE_CONNECTION, INVERTER_UNIT_ID, SharedMockModbus, load_fixture
from .fake_datamanager import PASSWORD, minted, serve_datamanager
from .test_config_flow import HOST, answer_family
from .test_datamanager_login import stored
from .test_several_inverters import add_datamanager_entry, choose

# The inverter model's header, its length as int + SF and as float, and the
# end of the map.
MODEL_HEADER = 40069
INT_LENGTH, FLOAT_LENGTH = 50, 60
MODEL_TYPE_ADDRESS = 215


def int_words() -> dict[int, int]:
    return dict(load_fixture("symo_datamanager.json")[INVERTER_UNIT_ID]["holding"])


def float_words(model_id: int = 113) -> dict[int, int]:
    """The same device as float: its inverter model is 10 registers longer."""
    words = int_words()
    behind = MODEL_HEADER + 2 + INT_LENGTH
    moved = {
        address + FLOAT_LENGTH - INT_LENGTH: word
        for address, word in words.items()
        if address >= behind
    }
    kept = {address: word for address, word in words.items() if address < MODEL_HEADER}
    model = [model_id, FLOAT_LENGTH, *[0] * FLOAT_LENGTH]
    return (
        kept
        | {MODEL_HEADER + offset: word for offset, word in enumerate(model)}
        | moved
        | {MODEL_TYPE_ADDRESS: 1}
    )


class FloatDatamanager(SharedMockModbus):
    """Switches as Fronius describes it: the type, confirmed by a 6 right after."""

    def __init__(self) -> None:
        super().__init__({INVERTER_UNIT_ID: {"holding": float_words()}})
        self.writes: list[tuple[int, int, int]] = []
        self.switches = True

    def __call__(self, params, **options):
        connection = super().__call__(params, **options)
        for unit_id in self._fixture:
            unit = connection.for_unit(unit_id)
            unit.on_write(
                lambda event, unit=unit, unit_id=unit_id: self._wrote(
                    unit, unit_id, event
                )
            )
        return connection

    def _wrote(self, unit, unit_id: int, event) -> None:
        for offset, value in enumerate(event.values):
            self.writes.append((unit_id, event.address + offset, int(value)))
        confirmed = [value for *_unit, value in self.writes[-2:]] == [2, 6]
        if confirmed and self.switches:
            for spaces in self._fixture.values():
                spaces["holding"] = int_words()
            unit.holding.clear()
            unit.load_raw({"holding": int_words()})


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


@pytest.fixture
def float_device(hass, monkeypatch) -> FloatDatamanager:
    shared = FloatDatamanager()
    monkeypatch.setattr(CORE_CONNECTION, shared)
    monkeypatch.setattr(inverter_choice, "MODEL_TYPE_RETRY_SECONDS", 0)
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    monkeypatch.setattr(datamanager, "read_inverter_units", lambda host: (1,))

    async def _skip_setup(hass, entry):
        return True

    monkeypatch.setattr(fronius_modbus, "async_setup_entry", _skip_setup)
    return shared


async def submit(hass, role: str = "none") -> dict:
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    return await hass.config_entries.flow.async_configure(
        form["flow_id"], {"host": HOST, "scan_interval": 10, "api_username": role}
    )


async def answer(hass, form: dict, data: dict[str, Any] | None = None) -> dict:
    return await hass.config_entries.flow.async_configure(form["flow_id"], data or {})


SWITCH = [(1, MODEL_TYPE_ADDRESS, 2), (1, MODEL_TYPE_ADDRESS, 6)]


async def test_the_model_type_a_unit_serves(float_device, connection):
    unit = connection.for_unit(9)
    unit.load_raw({"holding": float_words()})
    assert await FroniusInverter.async_model_type(unit) == "float"
    unit.holding.clear()
    unit.load_raw({"holding": int_words()})
    assert await FroniusInverter.async_model_type(unit) == "int"


async def test_a_meter_has_no_inverter_model(connection, meter_unit):
    assert await FroniusInverter.async_model_type(meter_unit) is None


async def test_float_is_asked_about_before_anything_is_written(hass, float_device):
    form = await submit(hass)

    assert (form["type"], form["step_id"]) == (FlowResultType.FORM, "model_type")
    assert form["description_placeholders"] == {"host": HOST}
    assert float_device.writes == []
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize("model_id", [111, 113])
async def test_the_confirmed_switch_is_two_writes(hass, float_device, model_id):
    float_device._fixture[1]["holding"] = float_words(model_id)
    form = await submit(hass)

    result = await answer(hass, form)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert float_device.writes == SWITCH


async def test_a_device_that_does_not_switch_creates_nothing(hass, float_device):
    float_device.switches = False
    form = await submit(hass)

    result = await answer(hass, form)

    assert result["step_id"] == "model_type"
    assert result["errors"] == {"base": "model_type_not_switched"}
    assert hass.config_entries.async_entries(DOMAIN) == []
    float_device.switches = True
    result = await answer(hass, form)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("error", [IllegalDataValueError, ModbusTimeoutError])
async def test_a_refused_write_creates_nothing(hass, float_device, error):
    form = await submit(hass)
    for connection in float_device.connections:
        connection.for_unit(1).fail_requests(error())
    float_device.fail_requests(1, error())

    result = await answer(hass, form)

    assert result["errors"] == {"base": "model_type_not_switched"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_split_phase_float_device_is_not_offered_a_switch(
    hass, float_device
):
    """Its int model 102 is not read here; the switch would leave nothing to read."""
    float_device._fixture[1]["holding"] = float_words(112)

    result = await submit(hass)

    assert result.get("step_id") != "model_type"
    assert float_device.writes == []


async def test_int_is_not_asked_about(hass, float_device):
    float_device._fixture[1]["holding"] = int_words()

    result = await submit(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert float_device.writes == []


async def test_a_silent_unit_is_no_float_device(hass, float_device):
    float_device.fail_requests(1, ModbusTimeoutError())

    result = await submit(hass)

    assert result["errors"] == {"base": "cannot_connect_modbus"}
    assert float_device.writes == []


@pytest.mark.parametrize("family", [DeviceFamily.OTHER, DeviceFamily.UNKNOWN])
async def test_another_device_is_never_switched(
    hass, float_device, monkeypatch, family
):
    """A GEN24 is asked and switched over its web interface."""
    answer_family(monkeypatch, family)

    result = await submit(hass)

    assert result["errors"] == {"base": "cannot_connect_modbus"}
    assert float_device.writes == []


async def test_the_login_comes_after_the_switch(hass, float_device, monkeypatch):
    serve_datamanager(monkeypatch)
    form = await submit(hass, "technician")
    assert form["step_id"] == "model_type"

    form = await answer(hass, form)
    assert form["step_id"] == "user_password_service"
    result = await answer(hass, form, {"api_password": PASSWORD})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert await stored(hass) == minted()
    assert float_device.writes == SWITCH


async def test_the_chosen_inverter_is_the_one_written(hass, float_device, monkeypatch):
    float_device._fixture[2] = {"holding": float_words()}
    monkeypatch.setattr(datamanager, "read_inverter_units", lambda host: (1, 2))
    form = await submit(hass)
    assert form["step_id"] == "inverter"

    form = await choose(hass, form, "2")
    assert form["step_id"] == "model_type"
    result = await answer(hass, form)

    assert result["data"][CONF_INVERTER_UNIT_ID] == 2
    assert float_device.writes == [(2, 215, 2), (2, 215, 6)]


async def test_a_technician_beside_the_owner_is_refused_before_a_switch(
    hass, float_device, monkeypatch
):
    """The role would be refused after the switch: nothing is written for it."""
    serve_datamanager(monkeypatch)
    float_device._fixture[2] = {"holding": float_words()}
    monkeypatch.setattr(datamanager, "read_inverter_units", lambda host: (1, 2))
    add_datamanager_entry(hass, 1)
    form = await submit(hass, "technician")

    result = await choose(hass, form, "2")

    assert result["errors"] == {"base": "datamanager_login_on_owner"}
    assert float_device.writes == []


async def test_a_host_taken_while_the_question_is_open_is_not_written(
    hass, float_device
):
    form = await submit(hass)
    add_datamanager_entry(hass, 1)

    result = await answer(hass, form)

    assert result["reason"] == "already_configured"
    assert float_device.writes == []


@pytest.mark.parametrize("kind", ["reconfigure", "options", "reauth"])
async def test_an_existing_entry_is_never_switched(hass, float_device, kind):
    entry = add_datamanager_entry(hass, 1)
    flow = hass.config_entries.flow
    if kind == "reconfigure":
        form = await entry.start_reconfigure_flow(hass)
    elif kind == "reauth":
        form = await entry.start_reauth_flow(hass)
    else:
        flow = hass.config_entries.options
        form = await flow.async_init(entry.entry_id)
    typed = {"api_username": "none"}
    if kind != "reauth":
        typed |= {"host": HOST, "scan_interval": 25}

    result = await flow.async_configure(form["flow_id"], typed)

    assert result["errors"] == {"base": "cannot_connect_modbus"}
    assert float_device.writes == []
