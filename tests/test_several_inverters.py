"""Several inverters on one Datamanager, each an entry of its own on one host."""

from typing import Any

from modbus_connection import GatewayTargetError
import pytest

from custom_components import fronius_modbus
from custom_components.fronius_modbus import config_flow, datamanager, discovery
from custom_components.fronius_modbus.const import (
    CONF_DATAMANAGER,
    CONF_INVERTER_UNIT_ID,
    DOMAIN,
    entry_title,
    entry_title_after,
    entry_unique_id,
    instance_key,
)
from custom_components.fronius_modbus.datamanager import (
    DeviceFamily,
    async_on_datamanager,
)
from custom_components.fronius_modbus.host_sharing import host_holder, unique_id_for
from custom_components.fronius_modbus.inverter_choice import async_free_units
from custom_components.fronius_modbus.token_store import async_get_token_store
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from .conftest import INVERTER_UNIT_ID
from .ha_test_plugin import MockConfigEntry
from .test_config_flow import answer_family, discover, discovered, shown_defaults

HOST = "192.0.2.10"
OTHER_HOST = "192.0.2.20"


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


def inverter(
    unit: int = 1, datamanager: bool | None = True, host: str = HOST
) -> dict[str, Any]:
    """The settings of one inverter; ``datamanager`` None is an entry never asked."""
    settings = {"host": host, "inverter_modbus_unit_id": unit, "api_username": "none"}
    return (
        settings if datamanager is None else settings | {CONF_DATAMANAGER: datamanager}
    )


def add_entry(hass, settings: dict[str, Any], unique_id: str | None, **more: Any):
    """An entry with these settings and the unique id it was stored with."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=settings,
        unique_id=unique_id,
        version=1,
        minor_version=14,
        **more,
    )
    entry.add_to_hass(hass)
    return entry


# -- the identity of an entry ---------------------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [
        inverter(1, datamanager=None),
        inverter(7, datamanager=None),
        inverter(7, datamanager=False),
        inverter(1, datamanager=True),
        {"host": HOST, CONF_DATAMANAGER: True},
        {"host": HOST},
        {"host": " 192.0.2.10 "},
    ],
)
def test_the_unique_id_stays_the_host(settings):
    assert entry_unique_id(settings) == HOST


@pytest.mark.parametrize("unit", [2, 100])
def test_a_datamanager_inverter_is_host_and_unit(unit):
    assert entry_unique_id(inverter(unit)) == f"{HOST}#{unit}"


GEN24 = inverter(1, datamanager=False)
FIRST = inverter(1)
SECOND = inverter(2)


@pytest.mark.parametrize(
    ("held", "stored_id", "newcomer", "shared"),
    [
        (GEN24, HOST, GEN24, False),
        (GEN24, HOST, FIRST, False),
        (GEN24, HOST, SECOND, False),
        (GEN24, None, GEN24, False),
        (GEN24, None, SECOND, False),
        (FIRST, HOST, GEN24, False),
        (FIRST, HOST, FIRST, False),
        (FIRST, HOST, SECOND, True),
        (SECOND, f"{HOST}#2", GEN24, False),
        (SECOND, f"{HOST}#2", FIRST, True),
        (SECOND, f"{HOST}#2", SECOND, False),
        # 0.1.9 set none; another entry's number is still not free.
        (FIRST, None, SECOND, True),
        (SECOND, None, FIRST, True),
        (SECOND, None, SECOND, False),
        (SECOND, None, GEN24, False),
        # Stored before the device was known as a Datamanager. Home Assistant
        # replaces an entry whose unique id a new one brings.
        (SECOND, HOST, FIRST, False),
        (SECOND, HOST, SECOND, False),
        (SECOND, HOST, inverter(3), True),
    ],
)
async def test_who_may_share_a_host(hass, held, stored_id, newcomer, shared):
    holder = add_entry(hass, held, stored_id)

    assert host_holder(hass, newcomer) is (None if shared else holder)


async def test_an_ignored_card_keeps_its_host_from_a_move_only(hass):
    """A new setup takes the card's place, as Home Assistant has it."""
    card = add_entry(hass, {}, HOST, source=SOURCE_IGNORE)
    moving = add_entry(hass, FIRST | {"host": OTHER_HOST}, OTHER_HOST)

    assert host_holder(hass, FIRST) is None
    assert host_holder(hass, FIRST, moving) is card


async def test_an_ignored_card_does_not_hide_the_entry_on_its_host(hass):
    add_entry(hass, {}, HOST, source=SOURCE_IGNORE)
    holder = add_entry(hass, GEN24, None)

    assert host_holder(hass, GEN24) is holder


@pytest.mark.parametrize(
    ("data", "unique_id"),
    [(GEN24, HOST), (GEN24, None), (inverter(7, datamanager=None), HOST)],
)
async def test_an_existing_entry_keeps_its_unique_id(hass, data, unique_id):
    """Asked again with its own settings, as Reconfigure does."""
    entry = add_entry(hass, data, unique_id)

    assert unique_id_for(hass, data, entry) == HOST
    assert host_holder(hass, data, entry) is None


async def test_the_second_entry_of_0_1_9_keeps_the_host_it_has(hass):
    """Asked on its own it is a Datamanager only once it ran without the web API."""
    add_entry(hass, FIRST, HOST)
    second = add_entry(hass, inverter(2, datamanager=None), None)

    assert host_holder(hass, dict(second.data), second) is None
    assert unique_id_for(hass, dict(second.data), second) == f"{HOST}#2"


@pytest.mark.parametrize(
    ("held", "moves"), [(None, True), (FIRST, True), (SECOND, False), (GEN24, False)]
)
async def test_an_inverter_moves_to_another_address(hass, held, moves):
    """The device there decides; with no entry there it is taken for the same device."""
    if held is not None:
        add_entry(
            hass,
            {**held, "host": OTHER_HOST},
            entry_unique_id(held | {"host": OTHER_HOST}),
        )
    entry = add_entry(hass, SECOND, f"{HOST}#2")
    moved = inverter(2, datamanager=None, host=OTHER_HOST)

    assert (host_holder(hass, moved, entry) is None) is moves
    if moves:
        assert unique_id_for(hass, moved, entry) == f"{OTHER_HOST}#2"


@pytest.mark.parametrize("unique_id", [f"{HOST}#2", None])
async def test_a_host_only_a_second_inverter_has_is_not_offered(hass, unique_id):
    add_entry(hass, SECOND, unique_id)

    result = await discover(hass, discovered())

    assert result["reason"] == "already_configured"


# -- an entry that learns its device --------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "stored", "family", "after"),
    [
        (2, HOST, DeviceFamily.DATAMANAGER, f"{HOST}#2"),
        (1, HOST, DeviceFamily.DATAMANAGER, HOST),
        (2, None, DeviceFamily.DATAMANAGER, None),
        (2, f"{HOST}#2", DeviceFamily.OTHER, HOST),
        (2, f"{HOST}#2", DeviceFamily.UNKNOWN, f"{HOST}#2"),
        (2, HOST, DeviceFamily.OTHER, HOST),
    ],
)
async def test_the_unique_id_follows_what_the_device_is(
    hass, monkeypatch, unit, stored, family, after
):
    """Without it another inverter of the host cannot take the host alone."""
    entry = add_entry(hass, inverter(unit, datamanager="#" in str(stored)), stored)
    answer_family(monkeypatch, family)

    await async_on_datamanager(hass, entry, HOST)

    assert entry.unique_id == after


async def test_a_taken_unique_id_is_left_to_its_entry(hass, monkeypatch):
    add_entry(hass, SECOND, f"{HOST}#2")
    entry = add_entry(hass, inverter(2, datamanager=None), HOST)
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)

    await async_on_datamanager(hass, entry, HOST)

    assert entry.unique_id == HOST


# -- which inverters there are --------------------------------------------------------


@pytest.mark.parametrize(
    ("listed", "taken", "free"),
    [
        ((1, 2, 100), (), [1, 2, 100]),
        ((1, 2), (1,), [2]),
        ((1, 2), (1, 2), []),
        ((), (1,), None),
    ],
)
async def test_the_inverters_without_an_entry(hass, monkeypatch, listed, taken, free):
    monkeypatch.setattr(datamanager, "read_inverter_units", lambda host: listed)
    for unit in taken:
        add_entry(hass, inverter(unit), entry_unique_id(inverter(unit)))
    add_entry(hass, inverter(1, host=OTHER_HOST), OTHER_HOST)

    assert await async_free_units(hass, HOST) == free


# -- the question which inverter ------------------------------------------------------


@pytest.fixture
def new_inverter(hass, datamanager_modbus, monkeypatch):
    """Set a Datamanager's host up by hand, without the web API, up to the first answer.

    A created entry is not set up: that is what the entry tests cover.
    """

    async def _skip_setup(hass, entry):
        return True

    monkeypatch.setattr(fronius_modbus, "async_setup_entry", _skip_setup)
    # Every inverter answers but number 9.
    for unit in (2, 3, 100):
        datamanager_modbus.add_unit(unit, like=INVERTER_UNIT_ID)
    datamanager_modbus.fail_requests(9, GatewayTargetError())

    async def _submit(
        *listed: int, family=DeviceFamily.DATAMANAGER, role: str = "none"
    ) -> dict:
        answer_family(monkeypatch, family)
        monkeypatch.setattr(datamanager, "read_inverter_units", lambda host: listed)
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        return await hass.config_entries.flow.async_configure(
            form["flow_id"], {"host": HOST, "scan_interval": 10, "api_username": role}
        )

    return _submit


async def choose(hass, form: dict, number: Any) -> dict:
    """Answer the question with this inverter number."""
    return await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_INVERTER_UNIT_ID: number}
    )


def add_datamanager_entry(hass, unit: int):
    return add_entry(
        hass,
        inverter(unit),
        entry_unique_id(inverter(unit)),
        title=entry_title(inverter(unit)),
    )


async def test_the_one_inverter_with_number_1_is_not_asked_for(hass, new_inverter):
    result = await new_inverter(1)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INVERTER_UNIT_ID] == 1
    assert result["data"][CONF_DATAMANAGER] is True
    assert result["result"].unique_id == HOST
    assert result["title"] == f"Fronius {HOST}"


@pytest.mark.parametrize(
    ("listed", "taken", "offered", "default"),
    [
        ((1, 2), (), ["1", "2"], "1"),
        ((1, 2, 100), (1,), ["2", "00"], "2"),
        ((2,), (), ["2"], "2"),
        ((100,), (), ["00"], "00"),
        ((1,), (2,), ["1"], "1"),
    ],
)
async def test_the_question_offers_the_inverters_without_an_entry(
    hass, new_inverter, listed, taken, offered, default
):
    for unit in taken:
        add_datamanager_entry(hass, unit)

    form = await new_inverter(*listed)

    assert (form["type"], form["step_id"]) == (FlowResultType.FORM, "inverter")
    assert shown_defaults(form) == {CONF_INVERTER_UNIT_ID: default}
    (selector,) = form["data_schema"].schema.values()
    assert selector.config["options"] == offered
    assert selector.config["custom_value"] is True


@pytest.mark.parametrize(("taken", "default"), [((), "1"), ((1,), "2"), ((2,), "1")])
async def test_a_datamanager_that_lists_none_is_asked_for_a_number(
    hass, new_inverter, taken, default
):
    """By night it answers without its inverters."""
    for unit in taken:
        add_datamanager_entry(hass, unit)

    form = await new_inverter()

    assert form["step_id"] == "inverter"
    assert shown_defaults(form) == {CONF_INVERTER_UNIT_ID: default}


@pytest.mark.parametrize(
    ("number", "unit", "unique_id", "title"),
    [
        ("1", 1, HOST, f"Fronius {HOST}"),
        ("2", 2, f"{HOST}#2", f"Fronius {HOST} #2"),
        (" 3 ", 3, f"{HOST}#3", f"Fronius {HOST} #3"),
        ("00", 100, f"{HOST}#100", f"Fronius {HOST} #00"),
        ("0", 100, f"{HOST}#100", f"Fronius {HOST} #00"),
    ],
)
async def test_the_chosen_inverter_is_set_up(
    hass, new_inverter, number, unit, unique_id, title
):
    form = await new_inverter(1, 2)

    result = await choose(hass, form, number)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INVERTER_UNIT_ID] == unit
    assert result["result"].unique_id == unique_id
    assert result["title"] == title


async def test_the_first_inverter_is_left_as_it_is(hass, new_inverter):
    """Home Assistant replaces an entry whose unique id a new one brings."""
    first = add_datamanager_entry(hass, 1)
    form = await new_inverter(1, 2)

    await choose(hass, form, "2")

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2 and first in entries


@pytest.mark.parametrize("number", ["100", "240", "-1", "1.5", "two", "", "1 2"])
async def test_no_inverter_has_such_a_number(hass, new_inverter, number):
    form = await new_inverter(1, 2)

    result = await choose(hass, form, number)

    assert result["step_id"] == "inverter"
    assert result["errors"] == {CONF_INVERTER_UNIT_ID: "invalid_inverter_number"}


async def test_a_number_that_does_not_answer_creates_nothing(hass, new_inverter):
    form = await new_inverter(1, 2)

    result = await choose(hass, form, "9")

    assert result["step_id"] == "inverter"
    assert result["errors"] == {CONF_INVERTER_UNIT_ID: "inverter_not_found"}
    assert shown_defaults(result) == {CONF_INVERTER_UNIT_ID: "9"}
    assert hass.config_entries.async_entries(DOMAIN) == []
    result = await choose(hass, form, "2")
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_inverter_of_another_maker_creates_nothing(
    hass, new_inverter, monkeypatch
):
    monkeypatch.setattr(config_flow, "SUPPORTED_MANUFACTURERS", ["Other"])
    form = await new_inverter(1, 2)

    result = await choose(hass, form, "2")

    assert result["step_id"] == "inverter"
    assert result["errors"] == {"base": "unsupported_hardware"}


async def test_a_typed_number_with_an_entry_is_not_set_up_again(hass, new_inverter):
    add_datamanager_entry(hass, 1)
    form = await new_inverter(1, 2)

    result = await choose(hass, form, "1")

    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_number_taken_while_the_question_is_open(hass, new_inverter):
    form = await new_inverter(1, 2)
    add_datamanager_entry(hass, 2)

    result = await choose(hass, form, "2")

    assert result["reason"] == "already_configured"


async def test_with_an_entry_for_every_inverter_nothing_is_asked(hass, new_inverter):
    add_datamanager_entry(hass, 1)
    add_datamanager_entry(hass, 2)

    result = await new_inverter(1, 2)

    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("family", [DeviceFamily.OTHER, DeviceFamily.UNKNOWN])
async def test_another_device_is_asked_nothing(hass, new_inverter, family):
    result = await new_inverter(1, 2, family=family)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INVERTER_UNIT_ID] == 1
    assert CONF_DATAMANAGER not in result["data"]


async def test_a_silent_datamanager_is_known_from_its_first_entry(hass, new_inverter):
    add_datamanager_entry(hass, 1)

    form = await new_inverter(family=DeviceFamily.UNKNOWN)

    assert form["step_id"] == "inverter"
    result = await choose(hass, form, "2")
    assert result["data"][CONF_DATAMANAGER] is True


async def test_another_device_at_a_datamanager_s_address_is_not_added(
    hass, new_inverter
):
    """The entry there is asked again at its next start and keeps the address."""
    add_datamanager_entry(hass, 2)

    result = await new_inverter(family=DeviceFamily.OTHER)

    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("family", [DeviceFamily.DATAMANAGER, DeviceFamily.UNKNOWN])
async def test_the_customer_is_refused_for_a_second_inverter_too(
    hass, new_inverter, family
):
    add_datamanager_entry(hass, 1)

    result = await new_inverter(1, 2, family=family, role="customer")

    assert result["errors"] == {"api_username": "datamanager_customer"}


async def test_the_question_without_its_settings_starts_over(hass, new_inverter):
    flow = config_flow.ConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}

    result = await flow.async_step_inverter({CONF_INVERTER_UNIT_ID: "1"})

    assert result["step_id"] == "user"


async def test_the_question_names_its_device(hass, new_inverter):
    form = await new_inverter(1, 2)

    assert form["description_placeholders"] == {"host": HOST}


@pytest.mark.parametrize("kind", ["reconfigure", "options", "reauth"])
async def test_an_existing_inverter_is_not_asked_for_again(hass, new_inverter, kind):
    add_datamanager_entry(hass, 1)
    second = add_datamanager_entry(hass, 2)
    flow = hass.config_entries.flow
    if kind == "reconfigure":
        form = await second.start_reconfigure_flow(hass)
    elif kind == "reauth":
        form = await second.start_reauth_flow(hass)
    else:
        flow = hass.config_entries.options
        form = await flow.async_init(second.entry_id)
    typed = {"api_username": "none"}
    if kind != "reauth":
        typed |= {"host": HOST, "scan_interval": 25}

    result = await flow.async_configure(form["flow_id"], typed)

    assert result["type"] in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT)
    assert result.get("reason") != "already_configured"
    settings = {**second.data, **second.options}
    assert settings[CONF_INVERTER_UNIT_ID] == 2
    assert second.unique_id == f"{HOST}#2"
    assert second.title == f"Fronius {HOST} #2"
    assert second.data[CONF_DATAMANAGER] is True
    assert CONF_DATAMANAGER not in second.options


@pytest.mark.parametrize("numbered", [True, False])
async def test_a_generated_title_follows_the_address(hass, numbered):
    """One from before the number was part of it is still a generated one."""
    second = add_datamanager_entry(hass, 2)
    title = f"Fronius {HOST} #2" if numbered else f"Fronius {HOST}"
    before = {**second.data}
    after = before | {"host": OTHER_HOST}

    assert entry_title_after(title, before, after) == f"Fronius {OTHER_HOST} #2"
    assert entry_title_after("Garage", before, after) == "Garage"


# -- discovery, a new address and the stored login --------------------------------------

SERIALS = {1: "11111111", 2: "22222222"}


def with_inverter(hass, unit: int):
    """An entry set up before, with the serial number its inverter reports."""
    entry = add_datamanager_entry(hass, unit)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{instance_key(entry.entry_id)}_inverter")},
        serial_number=SERIALS[unit],
    )
    return entry


@pytest.fixture
def moved(monkeypatch):
    """The Datamanager answers at another address now, each inverter on its unit."""
    monkeypatch.setattr(discovery, "MOVE_READ_RETRY_SECONDS", 0)

    async def _serial_at(_hass, host, _port, unit_id):
        return SERIALS[unit_id] if host == OTHER_HOST else None

    monkeypatch.setattr(discovery, "async_serial_at", _serial_at)


async def test_a_host_with_two_inverters_is_not_offered(hass):
    add_datamanager_entry(hass, 1)
    add_datamanager_entry(hass, 2)

    result = await discover(hass, discovered())

    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
async def test_each_inverter_follows_its_own_announcement(hass, moved, order):
    entries = {unit: with_inverter(hass, unit) for unit in (1, 2)}
    first, second = (entries[unit] for unit in order)

    await discover(hass, discovered(OTHER_HOST, SERIALS[order[0]]))

    assert (first.data["host"], second.data["host"]) == (OTHER_HOST, HOST)
    await discover(hass, discovered(OTHER_HOST, SERIALS[order[1]]))

    assert entries[1].unique_id == OTHER_HOST
    assert entries[2].unique_id == f"{OTHER_HOST}#2"
    assert entries[2].title == f"Fronius {OTHER_HOST} #2"


async def test_no_inverter_follows_to_an_address_another_device_has(hass, moved):
    entry = with_inverter(hass, 2)
    add_entry(hass, GEN24 | {"host": OTHER_HOST}, OTHER_HOST)

    await discover(hass, discovered(OTHER_HOST, SERIALS[2]))

    assert entry.data["host"] == HOST
    assert entry.unique_id == f"{HOST}#2"


async def test_a_login_stays_while_one_inverter_of_the_host_uses_it(hass):
    store = async_get_token_store(hass)
    entries = [
        add_entry(
            hass,
            inverter(unit) | {"api_username": "technician"},
            entry_unique_id(inverter(unit)),
        )
        for unit in (1, 2)
    ]
    await store.async_save_token(HOST, realm="r", token="t", user="technician")

    await hass.config_entries.async_remove(entries[0].entry_id)
    await hass.async_block_till_done()
    assert await store.async_load_token(HOST, "technician") is not None

    await hass.config_entries.async_remove(entries[1].entry_id)
    await hass.async_block_till_done()
    assert await store.async_load_token(HOST, "technician") is None
