"""Issue 187: a storage mode with a limit of 0 W read like a block mode after a restart."""

from modbus_connection import ServerDeviceFailureError
from modbus_connection.model.sunspec import scan
import pytest

from custom_components import fronius_modbus
from custom_components.fronius_modbus.const import (
    DOMAIN,
    STORAGE_EXT_CONTROL_MODE,
    entity_prefix,
)
from custom_components.fronius_modbus.fronius_modbus_api.exceptions import (
    ControlRefused,
    ControlUnavailable,
)
from custom_components.fronius_modbus.fronius_modbus_api.storage import (
    FULL_RATE_PCT,
    MODE_WRITE_GRACE_POLLS,
    ExtendedMode,
    StorageControl,
)
from custom_components.fronius_modbus.fronius_modbus_api.sunspec_models import (
    STORAGE_MODEL_ID,
    Storage,
)
from homeassistant.const import Platform
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er

from .conftest import INVERTER_UNIT_ID, SharedMockModbus
from .ha_test_plugin import mock_restore_cache_with_extra_data
from .test_e2e import make_entry, serve_the_public_endpoints, setup_entry, state_of

STORAGE_HEADER = 40343
STOR_CTL_MOD = STORAGE_HEADER + 5
OUT_W_RTE = STORAGE_HEADER + 12
IN_W_RTE = STORAGE_HEADER + 13
FULL = 10000
HALF_W = 5120
SELECT_ID = "select.fronius_storage_mode"

# Every mode left at a limit of 0 W by its own setter, the registers that
# leaves, the block mode the same registers read as on their own, and the
# same rates on another base mode, as another controller may write them.
AT_ZERO = [
    (
        ExtendedMode.PV_CHARGE_LIMIT,
        "set_charge_limit_w",
        (1, 0, FULL),
        ExtendedMode.BLOCK_CHARGING,
        (3, 0, FULL),
    ),
    (
        ExtendedMode.DISCHARGE_TO_GRID,
        "set_grid_discharge_power_w",
        (1, 0, FULL),
        ExtendedMode.BLOCK_CHARGING,
        (3, 0, FULL),
    ),
    (
        ExtendedMode.DISCHARGE_LIMIT,
        "set_discharge_limit_w",
        (2, FULL, 0),
        ExtendedMode.BLOCK_DISCHARGING,
        (3, FULL, 0),
    ),
    (
        ExtendedMode.CHARGE_FROM_GRID,
        "set_grid_charge_power_w",
        (2, FULL, 0),
        ExtendedMode.BLOCK_DISCHARGING,
        (3, FULL, 0),
    ),
    (
        ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT,
        "set_charge_limit_w",
        (3, 0, FULL),
        ExtendedMode.BLOCK_CHARGING,
        (1, 0, FULL),
    ),
    (
        ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT,
        "set_discharge_limit_w",
        (3, FULL, 0),
        ExtendedMode.BLOCK_DISCHARGING,
        (2, FULL, 0),
    ),
]
# A change of the 0 W rate that the mode itself still reads as.
_KEPT_MODE_CHANGE = {
    ExtendedMode.PV_CHARGE_LIMIT: FULL // 2,
    ExtendedMode.DISCHARGE_LIMIT: FULL // 2,
    ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT: FULL // 2,
    ExtendedMode.CHARGE_FROM_GRID: -FULL // 2,
    ExtendedMode.DISCHARGE_TO_GRID: -FULL // 2,
}
AT_ZERO_IDS = [f"{row[0].name}-{row[1]}" for row in AT_ZERO]


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    """Let Home Assistant load custom_components/fronius_modbus in every test here."""
    return enable_custom_integrations


def _hold(holding: dict[int, int], image: tuple[int, int, int]) -> None:
    mode, in_rate, out_rate = image
    holding[STOR_CTL_MOD] = mode
    holding[IN_W_RTE] = in_rate & 0xFFFF
    holding[OUT_W_RTE] = out_rate & 0xFFFF


def _zero_rate(image: tuple[int, int, int]) -> int:
    return IN_W_RTE if image[1] == 0 else OUT_W_RTE


async def _control(unit, *, read: bool = True) -> StorageControl:
    """A control as a start builds it: nothing written, the registers read once."""
    chain = await scan(unit, 40000)
    storage = Storage(unit, chain.first(STORAGE_MODEL_ID))
    control = StorageControl(
        storage, max_charge_rate_w=10240, max_discharge_rate_w=10240
    )
    if read:
        await storage.async_update()
        control.sync_from_device()
    return control


async def _poll(control: StorageControl, times: int = MODE_WRITE_GRACE_POLLS) -> None:
    for _ in range(times):
        await control._storage.async_update()
        control.sync_from_device()


async def _saved_at_zero(unit, mode, setter) -> dict:
    """What the select saves once a poll showed the mode at 0 W."""
    control = await _control(unit)
    await control.set_mode(mode)
    await getattr(control, setter)(0)
    await _poll(control)
    assert control.extended_mode is mode
    saved = control.saved()
    assert saved is not None
    return saved


def _writes(unit) -> list:
    events: list = []
    unit.on_write(events.append)
    return events


def _half_power_write(mode, image) -> list:
    """The one register write a setter at half the maximum makes: the 0 W rate."""
    grid = mode in (ExtendedMode.CHARGE_FROM_GRID, ExtendedMode.DISCHARGE_TO_GRID)
    raw = -FULL // 2 if grid else FULL // 2
    return [(_zero_rate(image), [raw & 0xFFFF])]


def _written(events) -> list:
    return [(event.address, list(event.values)) for event in events]


@pytest.mark.parametrize(
    ("mode", "setter", "image", "shared", "foreign"), AT_ZERO, ids=AT_ZERO_IDS
)
async def test_a_mode_at_0_w_is_kept_across_a_restart_on_unchanged_registers(
    inverter_unit, mode, setter, image, shared, foreign
):
    """Issue 187: PV Charge Limit at 0 W came back as Block Charging, its limit gone."""
    saved = await _saved_at_zero(inverter_unit, mode, setter)
    restarted = await _control(inverter_unit)
    assert restarted.extended_mode is shared
    writes = _writes(inverter_unit)

    restarted.restore(saved)
    await _poll(restarted)

    assert restarted.extended_mode is mode
    assert writes == []
    await getattr(restarted, setter)(HALF_W)
    assert _written(writes) == _half_power_write(mode, image)


@pytest.mark.parametrize(
    ("mode", "setter", "image", "shared", "foreign"), AT_ZERO, ids=AT_ZERO_IDS
)
async def test_registers_another_controller_changed_in_the_pause_keep_their_mode(
    inverter_unit, mode, setter, image, shared, foreign
):
    """evcc's hold on another base mode is the same rates, but not the image saved."""
    saved = await _saved_at_zero(inverter_unit, mode, setter)
    _hold(inverter_unit.holding, foreign)
    restarted = await _control(inverter_unit)
    writes = _writes(inverter_unit)

    restarted.restore(saved)
    await _poll(restarted)

    assert restarted.extended_mode is shared
    with pytest.raises(ControlRefused):
        await getattr(restarted, setter)(HALF_W)
    assert writes == []


@pytest.mark.parametrize(
    ("mode", "setter", "image", "shared", "foreign"), AT_ZERO, ids=AT_ZERO_IDS
)
async def test_a_kept_mode_lapses_for_good_once_the_registers_change(
    inverter_unit, mode, setter, image, shared, foreign
):
    """A 0 W that comes back after a change may be another controller's hold.

    The change keeps the mode, so only the kept mode's own record can lapse.
    """
    saved = await _saved_at_zero(inverter_unit, mode, setter)
    restarted = await _control(inverter_unit)
    restarted.restore(saved)
    zero_rate = _zero_rate(image)
    inverter_unit.holding[zero_rate] = _KEPT_MODE_CHANGE[mode] & 0xFFFF
    await _poll(restarted, 1)
    inverter_unit.holding[zero_rate] = 0
    await _poll(restarted)
    writes = _writes(inverter_unit)

    assert restarted.extended_mode is shared
    with pytest.raises(ControlRefused):
        await getattr(restarted, setter)(HALF_W)
    assert writes == []


@pytest.mark.parametrize(
    ("mode", "setter", "image", "shared", "foreign"), AT_ZERO, ids=AT_ZERO_IDS
)
async def test_a_kept_mode_refuses_a_command_while_the_registers_cannot_be_read(
    inverter_unit, mode, setter, image, shared, foreign
):
    """Nothing but a live read can show that the image is still the one kept."""
    saved = await _saved_at_zero(inverter_unit, mode, setter)
    restarted = await _control(inverter_unit)
    restarted.restore(saved)
    inverter_unit.fail_read(_zero_rate(image), ServerDeviceFailureError())
    writes = _writes(inverter_unit)

    with pytest.raises(ControlUnavailable) as unavailable:
        await getattr(restarted, setter)(HALF_W)

    assert unavailable.value.key == "storage_mode_unverified"
    assert writes == []
    inverter_unit.fail_read(_zero_rate(image), None)
    await getattr(restarted, setter)(HALF_W)
    assert _written(writes) == _half_power_write(mode, image)


@pytest.mark.parametrize(
    ("mode", "setter", "image", "shared", "foreign"), AT_ZERO, ids=AT_ZERO_IDS
)
async def test_a_live_read_that_disproves_a_kept_mode_ends_it(
    inverter_unit, mode, setter, image, shared, foreign
):
    """Refused once, the kept mode let the same command through when the 0 W came back."""
    saved = await _saved_at_zero(inverter_unit, mode, setter)
    restarted = await _control(inverter_unit)
    restarted.restore(saved)
    _hold(inverter_unit.holding, foreign)
    writes = _writes(inverter_unit)
    with pytest.raises(ControlRefused):
        await getattr(restarted, setter)(HALF_W)
    _hold(inverter_unit.holding, image)

    with pytest.raises(ControlRefused):
        await getattr(restarted, setter)(HALF_W)
    assert writes == []
    assert restarted.saved() is None


@pytest.mark.parametrize(
    ("mode", "setter", "image", "shared", "foreign"), AT_ZERO, ids=AT_ZERO_IDS
)
async def test_a_mode_change_that_fails_half_way_ends_the_kept_mode(
    inverter_unit, mode, setter, image, shared, foreign
):
    """The rates may already be on the device: the kept claim no longer holds."""
    saved = await _saved_at_zero(inverter_unit, mode, setter)
    restarted = await _control(inverter_unit)
    restarted.restore(saved)
    write = restarted._storage.write

    async def mode_write_fails(name, value):
        if name == "stor_ctl_mod":
            raise ServerDeviceFailureError
        await write(name, value)

    restarted._storage.write = mode_write_fails
    with pytest.raises(ServerDeviceFailureError):
        await restarted.set_mode(ExtendedMode.AUTO)
    restarted._storage.write = write
    _hold(inverter_unit.holding, image)
    writes = _writes(inverter_unit)

    with pytest.raises(ControlRefused):
        await getattr(restarted, setter)(HALF_W)
    assert writes == []
    assert restarted.saved() is None


async def test_a_kept_mode_refuses_a_command_after_a_change_the_poll_missed(
    inverter_unit,
):
    saved = await _saved_at_zero(
        inverter_unit, ExtendedMode.PV_CHARGE_LIMIT, "set_charge_limit_w"
    )
    restarted = await _control(inverter_unit)
    restarted.restore(saved)
    _hold(inverter_unit.holding, (3, 0, FULL))
    writes = _writes(inverter_unit)

    with pytest.raises(ControlRefused):
        await restarted.set_charge_limit_w(HALF_W)
    assert writes == []


async def test_a_0_w_no_poll_showed_yet_is_not_saved(inverter_unit):
    control = await _control(inverter_unit)
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(0)

    assert control.saved() is None


async def test_a_0_w_the_polls_showed_only_as_the_old_rate_is_not_saved(
    inverter_unit,
):
    """A late poll with the old rate still reads as the mode and confirmed the write.

    Saved anyway, a restart took the image for Home Assistant's own although
    no poll had shown it: the inverter may never have applied it.
    """
    control = await _control(inverter_unit)
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await _poll(control)
    await control.set_charge_limit_w(0)
    inverter_unit.holding[IN_W_RTE] = FULL
    await _poll(control, 1)

    assert control.extended_mode is ExtendedMode.PV_CHARGE_LIMIT
    assert control.saved() is None


@pytest.mark.parametrize(
    ("mode", "setter", "image", "shared", "foreign"), AT_ZERO, ids=AT_ZERO_IDS
)
async def test_a_0_w_that_returns_after_a_change_is_not_saved(
    inverter_unit, mode, setter, image, shared, foreign
):
    """The 0 W that comes back may be another controller's hold, not the one confirmed."""
    control = await _control(inverter_unit)
    await control.set_mode(mode)
    await getattr(control, setter)(0)
    await _poll(control)
    zero_rate = _zero_rate(image)
    inverter_unit.holding[zero_rate] = _KEPT_MODE_CHANGE[mode] & 0xFFFF
    await _poll(control, 1)
    inverter_unit.holding[zero_rate] = 0
    await _poll(control)

    assert control.saved() is None


async def test_a_0_w_found_in_place_is_not_saved_before_a_poll(inverter_unit):
    """Nothing to write is still a choice no poll has shown yet."""
    _hold(inverter_unit.holding, (1, 0, FULL))
    control = await _control(inverter_unit)
    writes = _writes(inverter_unit)
    await control.set_mode(ExtendedMode.DISCHARGE_TO_GRID)

    assert writes == []
    assert control.saved() is None
    await _poll(control, 1)
    assert control.saved() == {
        "mode": "DISCHARGE_TO_GRID",
        "image": [1, 0.0, FULL_RATE_PCT],
    }


async def test_a_mode_chosen_over_a_saved_one_waits_for_a_poll(inverter_unit):
    """Discharge to Grid writes nothing over PV Charge Limit at 0 W."""
    control = await _control(inverter_unit)
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(0)
    await _poll(control)
    assert control.saved() is not None

    await control.set_mode(ExtendedMode.DISCHARGE_TO_GRID)

    assert control.saved() is None


async def test_a_limit_read_but_not_judged_yet_is_not_saved(inverter_unit):
    control = await _control(inverter_unit)
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(HALF_W)
    await _poll(control)
    await control.set_charge_limit_w(0)

    await control._storage.async_update()

    assert control.saved() is None


async def test_a_change_read_but_not_judged_yet_saves_nothing(inverter_unit):
    """The coordinator judges the storage after the whole poll; a save may come between."""
    control = await _control(inverter_unit)
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(0)
    await _poll(control)
    inverter_unit.holding[IN_W_RTE] = FULL // 2

    await control._storage.async_update()

    assert control.saved() is None


async def test_a_mode_waiting_for_the_first_full_read_is_saved_again(inverter_unit):
    """A second reload before the storage answered lost the mode kept for it."""
    saved = await _saved_at_zero(
        inverter_unit, ExtendedMode.PV_CHARGE_LIMIT, "set_charge_limit_w"
    )
    restarted = await _control(inverter_unit, read=False)

    restarted.restore(saved)

    assert restarted.saved() == saved


async def test_a_mode_its_registers_name_alone_saves_nothing(inverter_unit):
    control = await _control(inverter_unit)
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(HALF_W)
    await _poll(control)

    assert control.extended_mode is ExtendedMode.PV_CHARGE_LIMIT
    assert control.saved() is None


async def test_a_mode_another_controller_took_over_saves_nothing(inverter_unit):
    control = await _control(inverter_unit)
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(HALF_W)
    await _poll(control)
    inverter_unit.holding[IN_W_RTE] = 0
    await _poll(control)

    assert control.extended_mode is ExtendedMode.BLOCK_CHARGING
    assert control.saved() is None


async def test_nothing_is_saved_while_a_write_runs(inverter_unit):
    """The registers may already be half way to the next mode."""
    saved_during = []
    control = await _control(inverter_unit)
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(0)
    await _poll(control)
    write = control._storage.write

    async def save_then_write(name, value):
        saved_during.append(control.saved())
        await write(name, value)

    control._storage.write = save_then_write
    await control.set_mode(ExtendedMode.AUTO)

    assert saved_during
    assert all(saved is None for saved in saved_during)


async def test_a_mode_saved_before_the_first_full_read_waits_for_it(inverter_unit):
    saved = await _saved_at_zero(
        inverter_unit, ExtendedMode.PV_CHARGE_LIMIT, "set_charge_limit_w"
    )
    restarted = await _control(inverter_unit, read=False)

    restarted.restore(saved)
    await _poll(restarted)

    assert restarted.extended_mode is ExtendedMode.PV_CHARGE_LIMIT


async def test_a_mode_chosen_before_the_restore_wins(inverter_unit):
    """Discharge to Grid writes nothing over these registers and is still a choice."""
    saved = await _saved_at_zero(
        inverter_unit, ExtendedMode.PV_CHARGE_LIMIT, "set_charge_limit_w"
    )
    restarted = await _control(inverter_unit)
    writes = _writes(inverter_unit)
    await restarted.set_mode(ExtendedMode.DISCHARGE_TO_GRID)

    restarted.restore(saved)
    await _poll(restarted)

    assert writes == []
    assert restarted.extended_mode is ExtendedMode.DISCHARGE_TO_GRID


async def test_a_limit_set_before_the_restore_wins(inverter_unit):
    """A restore judged before the next poll took the old registers for its own."""
    saved = await _saved_at_zero(
        inverter_unit, ExtendedMode.PV_CHARGE_LIMIT, "set_charge_limit_w"
    )
    restarted = await _control(inverter_unit)
    await restarted.set_discharge_limit_w(HALF_W)

    restarted.restore(saved)

    assert restarted.extended_mode is ExtendedMode.BLOCK_CHARGING


async def test_a_restore_after_a_change_since_the_first_read_is_ignored(
    inverter_unit,
):
    saved = await _saved_at_zero(
        inverter_unit, ExtendedMode.PV_CHARGE_LIMIT, "set_charge_limit_w"
    )
    restarted = await _control(inverter_unit)
    inverter_unit.holding[IN_W_RTE] = FULL // 2
    await _poll(restarted, 1)
    inverter_unit.holding[IN_W_RTE] = 0
    await _poll(restarted, 1)

    restarted.restore(saved)

    assert restarted.extended_mode is ExtendedMode.BLOCK_CHARGING


async def test_a_kept_mode_is_saved_again_for_the_next_restart(inverter_unit):
    saved = await _saved_at_zero(
        inverter_unit, ExtendedMode.PV_CHARGE_LIMIT, "set_charge_limit_w"
    )
    restarted = await _control(inverter_unit)
    restarted.restore(saved)
    assert restarted.saved() == saved
    await _poll(restarted)

    assert restarted.saved() == saved


@pytest.mark.parametrize(
    "saved",
    [
        {},
        {"mode": "PV_CHARGE_LIMIT"},
        {"mode": "NO_SUCH_MODE", "image": [1, 0.0, 100.0]},
        {"mode": "PV_CHARGE_LIMIT", "image": [1, 0.0]},
        {"mode": "PV_CHARGE_LIMIT", "image": "1, 0, 100"},
        {"mode": "PV_CHARGE_LIMIT", "image": [1, "0", 100.0]},
        {"mode": "PV_CHARGE_LIMIT", "image": [1.5, 0.0, 100.0]},
        {"mode": 1, "image": [1, 0.0, 100.0]},
        # Charge from Grid never holds these registers.
        {"mode": "CHARGE_FROM_GRID", "image": [1, 0.0, 100.0]},
        {"mode": "PV_CHARGE_LIMIT", "image": [1, 10**401, 100.0]},
        {"mode": "PV_CHARGE_LIMIT", "image": [1, float("nan"), 100.0]},
        {"mode": "PV_CHARGE_LIMIT", "image": [1, 0.0, 100.5]},
        ["PV_CHARGE_LIMIT", [1, 0.0, 100.0]],
        None,
    ],
)
async def test_saved_data_that_names_no_mode_of_these_registers_is_ignored(
    inverter_unit, saved
):
    _hold(inverter_unit.holding, (1, 0, FULL))
    restarted = await _control(inverter_unit)

    restarted.restore(saved)

    assert restarted.extended_mode is ExtendedMode.BLOCK_CHARGING


def _register_the_select(hass, entry) -> None:
    """The registry keeps the entity across a restart; its id names the saved state."""
    er.async_get(hass).async_get_or_create(
        "select",
        DOMAIN,
        f"{entity_prefix(entry.entry_id)}_ext_control_mode",
        config_entry=entry,
        suggested_object_id="fronius_storage_mode",
    )


def _count_writes(monkeypatch) -> list:
    """Every Modbus write from the first connection on."""
    writes: list = []
    connect = SharedMockModbus.__call__

    def counted(self, params, **kwargs):
        connection = connect(self, params, **kwargs)
        for unit_id in self._fixture:
            connection.for_unit(unit_id).on_write(writes.append)
        return connection

    monkeypatch.setattr(SharedMockModbus, "__call__", counted)
    return writes


async def _restart_with(
    hass, mock_modbus, monkeypatch, symo_gen24, image, saved, *, also=()
):
    """Start on these registers with the select's saved data and other saved states."""
    serve_the_public_endpoints(mock_modbus, monkeypatch)
    writes = _count_writes(monkeypatch)
    _hold(symo_gen24[INVERTER_UNIT_ID]["holding"], image)
    entry = make_entry(hass, api_username="none")
    _register_the_select(hass, entry)
    states = []
    for key, entity_id, state, data in also:
        er.async_get(hass).async_get_or_create(
            entity_id.split(".")[0],
            DOMAIN,
            f"{entity_prefix(entry.entry_id)}_{key}",
            config_entry=entry,
            suggested_object_id=entity_id.split(".")[1],
        )
        states.append((State(entity_id, state), data))
    if saved is not None:
        shown = STORAGE_EXT_CONTROL_MODE[ExtendedMode[saved["mode"]]]
        states.append((State(SELECT_ID, shown), saved))
    if states:
        mock_restore_cache_with_extra_data(hass, states)
    await setup_entry(hass, entry)
    return entry, writes


def _select_last(monkeypatch) -> None:
    """The platforms start side by side; the select is first only by its place."""
    later_select = [p for p in fronius_modbus.PLATFORMS if p is not Platform.SELECT]
    monkeypatch.setattr(fronius_modbus, "PLATFORMS", [*later_select, Platform.SELECT])


# The registers a mode left at 0 W, as the select shows it, and the limit it opens.
RESTARTED_AT_ZERO = [
    ((1, 0, FULL), ExtendedMode.PV_CHARGE_LIMIT, "pv_charge_limit", "charge_limit"),
    ((2, FULL, 0), ExtendedMode.DISCHARGE_LIMIT, "discharge_limit", "discharge_limit"),
    (
        (3, 0, FULL),
        ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT,
        "pv_charge_and_discharge_limit",
        "charge_limit",
    ),
    (
        (3, FULL, 0),
        ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT,
        "pv_charge_and_discharge_limit",
        "discharge_limit",
    ),
    (
        (2, FULL, 0),
        ExtendedMode.CHARGE_FROM_GRID,
        "charge_from_grid",
        "grid_charge_power",
    ),
    (
        (1, 0, FULL),
        ExtendedMode.DISCHARGE_TO_GRID,
        "discharge_to_grid",
        "grid_discharge_power",
    ),
]


def _saved(mode: ExtendedMode, image: tuple[int, int, int]) -> dict:
    return {"mode": mode.name, "image": [image[0], image[1] / 100, image[2] / 100]}


@pytest.mark.e2e
@pytest.mark.parametrize(("image", "mode", "shown", "limit"), RESTARTED_AT_ZERO)
async def test_a_mode_at_0_w_stays_after_a_restart_without_a_write(
    hass, mock_modbus, monkeypatch, symo_gen24, image, mode, shown, limit
):
    """Issue 187: PV Charge Limit at 0 W came back as block_charging, its limit gone."""
    entry, writes = await _restart_with(
        hass, mock_modbus, monkeypatch, symo_gen24, image, _saved(mode, image)
    )

    assert state_of(hass, entry, "ext_control_mode", "select") == shown
    assert float(state_of(hass, entry, limit, "number")) == 0
    for _ in range(MODE_WRITE_GRACE_POLLS):
        await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entry, "ext_control_mode", "select") == shown
    assert writes == []


@pytest.mark.e2e
async def test_another_controller_s_hold_charge_in_the_pause_is_shown_as_such(
    hass, mock_modbus, monkeypatch, symo_gen24
):
    entry, writes = await _restart_with(
        hass,
        mock_modbus,
        monkeypatch,
        symo_gen24,
        (1, 0, FULL),
        _saved(ExtendedMode.PV_CHARGE_LIMIT, (1, FULL // 2, FULL)),
    )

    assert state_of(hass, entry, "ext_control_mode", "select") == "block_charging"
    assert state_of(hass, entry, "charge_limit", "number") == "unavailable"
    assert writes == []


@pytest.mark.e2e
async def test_without_saved_data_the_registers_decide(
    hass, mock_modbus, monkeypatch, symo_gen24
):
    """The first start after the update: the select saved nothing before."""
    entry, _ = await _restart_with(
        hass, mock_modbus, monkeypatch, symo_gen24, (1, 0, FULL), None
    )

    assert state_of(hass, entry, "ext_control_mode", "select") == "block_charging"
    assert state_of(hass, entry, "charge_limit", "number") == "unavailable"


@pytest.mark.e2e
async def test_a_number_set_up_before_the_select_follows_the_mode_it_keeps(
    hass, mock_modbus, monkeypatch, symo_gen24
):
    _select_last(monkeypatch)
    entry, _ = await _restart_with(
        hass,
        mock_modbus,
        monkeypatch,
        symo_gen24,
        (1, 0, FULL),
        _saved(ExtendedMode.PV_CHARGE_LIMIT, (1, 0, FULL)),
    )
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()

    assert state_of(hass, entry, "charge_limit", "number") == "0.0"


@pytest.mark.e2e
async def test_two_low_energy_readings_are_no_reset_when_the_mode_is_kept(
    hass, mock_modbus, monkeypatch, symo_gen24
):
    """Notifying the entities at the restore made the energy total see one reading twice."""
    _select_last(monkeypatch)
    total = "sensor.fronius_energy_total"
    entry, _ = await _restart_with(
        hass,
        mock_modbus,
        monkeypatch,
        symo_gen24,
        (1, 0, FULL),
        _saved(ExtendedMode.PV_CHARGE_LIMIT, (1, 0, FULL)),
        also=[
            (
                "acenergy",
                total,
                "999999999999.0",
                {"native_value": 999999999999.0, "native_unit_of_measurement": "Wh"},
            )
        ],
    )
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(total).state == "999999999999.0"


@pytest.mark.e2e
async def test_pv_charge_limit_at_0_w_stays_after_a_reload(
    hass, mock_modbus, monkeypatch, symo_gen24
):
    serve_the_public_endpoints(mock_modbus, monkeypatch)
    entry = make_entry(hass, api_username="none")
    await setup_entry(hass, entry)
    select_id = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{entity_prefix(entry.entry_id)}_ext_control_mode"
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select_id, "option": "pv_charge_limit"},
        blocking=True,
    )
    number_id = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{entity_prefix(entry.entry_id)}_charge_limit"
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": number_id, "value": 0}, blocking=True
    )
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(select_id).state == "pv_charge_limit"
    # The reload connects anew; the device still holds what was written.
    _hold(symo_gen24[INVERTER_UNIT_ID]["holding"], (1, 0, FULL))

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(select_id).state == "pv_charge_limit"
    assert hass.states.get(number_id).state == "0.0"
