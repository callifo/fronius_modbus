"""The storage mode automaton and its write sequences, watched through the mock's write events."""

import asyncio
import contextlib
import logging
from types import SimpleNamespace

from modbus_connection import ServerDeviceFailureError
from modbus_connection.model.sunspec import scan
import pytest

from custom_components.fronius_modbus.fronius_modbus_api import (
    storage as storage_module,
)
from custom_components.fronius_modbus.fronius_modbus_api.exceptions import (
    ControlRefused,
)
from custom_components.fronius_modbus.fronius_modbus_api.storage import (
    CHARGE_LIMIT_MODES,
    DISCHARGE_LIMIT_MODES,
    MODE_WRITE_GRACE_POLLS,
    ExtendedMode,
    StorageControl,
)
from custom_components.fronius_modbus.fronius_modbus_api.sunspec_models import (
    STORAGE_MODEL_ID,
    Storage,
)
from custom_components.fronius_modbus.fronius_modbus_api.writes import register_value

STORAGE_HEADER = 40343
STOR_CTL_MOD = STORAGE_HEADER + 5
MIN_RSV_PCT = STORAGE_HEADER + 7
OUT_W_RTE = STORAGE_HEADER + 12
IN_W_RTE = STORAGE_HEADER + 13


@pytest.fixture
async def control(inverter_unit):
    chain = await scan(inverter_unit, 40000)
    storage = Storage(inverter_unit, chain.first(STORAGE_MODEL_ID))
    await storage.async_update()
    control = StorageControl(
        storage, max_charge_rate_w=10240, max_discharge_rate_w=10240
    )
    control.sync_from_device()
    return control


@pytest.fixture
def writes(inverter_unit):
    events = []
    inverter_unit.on_write(events.append)
    return events


def _words(events, address):
    return [event.values[0] for event in events if event.address == address]


async def test_auto_on_the_device_reads_as_auto(control):
    assert control.extended_mode is ExtendedMode.AUTO
    assert control.control_mode == 0
    assert (control.charge_limit_pct, control.discharge_limit_pct) == (100.0, 100.0)
    assert (control.grid_charge_power_pct, control.grid_discharge_power_pct) == (
        0.0,
        0.0,
    )
    assert control.soc_minimum == 5


@pytest.mark.parametrize(
    ("stor_ctl_mod", "out_w_rte", "in_w_rte", "expected"),
    [
        (1, 10000, 0, ExtendedMode.BLOCK_CHARGING),
        (1, 10000, 10000, ExtendedMode.PV_CHARGE_LIMIT),
        (2, -3000 & 0xFFFF, 10000, ExtendedMode.CHARGE_FROM_GRID),
        (2, 10000, -3000 & 0xFFFF, ExtendedMode.DISCHARGE_TO_GRID),
        (3, 0, 10000, ExtendedMode.BLOCK_DISCHARGING),
        (2, 10000, 10000, ExtendedMode.DISCHARGE_LIMIT),
        (3, 10000, 10000, ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT),
    ],
)
async def test_the_extended_mode_is_derived_from_the_registers(
    inverter_unit, stor_ctl_mod, out_w_rte, in_w_rte, expected
):
    inverter_unit.holding[STOR_CTL_MOD] = stor_ctl_mod
    inverter_unit.holding[OUT_W_RTE] = out_w_rte
    inverter_unit.holding[IN_W_RTE] = in_w_rte
    chain = await scan(inverter_unit, 40000)
    storage = Storage(inverter_unit, chain.first(STORAGE_MODEL_ID))
    await storage.async_update()
    control = StorageControl(
        storage, max_charge_rate_w=10240, max_discharge_rate_w=10240
    )
    control.sync_from_device()
    assert control.extended_mode is expected


async def test_charge_from_grid_writes_mode_then_rates(control, writes, inverter_unit):
    inverter_unit.holding[IN_W_RTE] = 5000
    await control.set_mode(ExtendedMode.CHARGE_FROM_GRID)
    assert _words(writes, STOR_CTL_MOD) == [2]
    assert _words(writes, IN_W_RTE) == [10000]
    assert _words(writes, OUT_W_RTE) == [0]
    assert control.extended_mode is ExtendedMode.CHARGE_FROM_GRID


async def test_block_charging_after_grid_charging_raises_the_discharge_rate_first(
    control, writes
):
    """Upstream #126: both rates at or below zero for an instant is refused (exception 3)."""
    await control.set_mode(ExtendedMode.CHARGE_FROM_GRID)
    await control.set_grid_charge_power_w(2560)
    await control._storage.async_update()
    del writes[:]
    await control.set_mode(ExtendedMode.BLOCK_CHARGING)
    order = [event.address for event in writes]
    assert order.index(OUT_W_RTE) < order.index(IN_W_RTE)
    assert _words(writes, OUT_W_RTE) == [10000]
    assert _words(writes, IN_W_RTE) == [0]


def _signed(word):
    return word - 0x10000 if word & 0x8000 else word


def _refused_windows(inverter_unit):
    """Every register image the firmware refuses (exception 3), as the writes pass it."""
    image = {
        address: inverter_unit.holding[address]
        for address in (STOR_CTL_MOD, IN_W_RTE, OUT_W_RTE)
    }
    refused = []

    def track(event):
        for offset, word in enumerate(event.values):
            image[event.address + offset] = word
        mode = image[STOR_CTL_MOD]
        in_rate, out_rate = _signed(image[IN_W_RTE]), _signed(image[OUT_W_RTE])
        if (in_rate <= 0 and out_rate <= 0) or (mode == 3 and -in_rate > out_rate):
            refused.append((mode, in_rate, out_rate))

    inverter_unit.on_write(track)
    return refused


# The grid modes start from a power set in them: the image the last poll misses.
GRID_POWER_SETTERS = {
    ExtendedMode.CHARGE_FROM_GRID: "set_grid_charge_power_w",
    ExtendedMode.DISCHARGE_TO_GRID: "set_grid_discharge_power_w",
}


@pytest.mark.parametrize("then", list(ExtendedMode))
@pytest.mark.parametrize("start", list(ExtendedMode))
async def test_a_mode_change_before_the_next_poll_passes_no_refused_window(
    control, inverter_unit, start, then
):
    """Upstream #126 again: the order came from the last poll, which a second change outran."""
    refused = _refused_windows(inverter_unit)

    await control.set_mode(start)
    if start in GRID_POWER_SETTERS:
        await getattr(control, GRID_POWER_SETTERS[start])(3000)
    await control.set_mode(then)

    assert refused == []


async def test_a_register_is_read_at_the_device_scale(control):
    """The rate the storage writes judge by is the percent the poll would show."""
    await control.set_mode(ExtendedMode.CHARGE_FROM_GRID)
    await control.set_grid_charge_power_w(2560)

    assert await register_value(control._storage, "out_w_rte") == -25.0


async def test_grid_charge_power_is_a_negative_discharge_rate(control, writes):
    await control.set_mode(ExtendedMode.CHARGE_FROM_GRID)
    await control.set_grid_charge_power_w(2560)
    assert _words(writes, OUT_W_RTE)[-1] == (-2500) & 0xFFFF
    await control._storage.async_update()
    assert control.grid_charge_power_pct == 25.0


# Each grid mode, the setter of the power it leaves to the user, and its register.
GRID_POWERS = [
    (ExtendedMode.CHARGE_FROM_GRID, "set_grid_charge_power_w", OUT_W_RTE),
    (ExtendedMode.DISCHARGE_TO_GRID, "set_grid_discharge_power_w", IN_W_RTE),
]


@pytest.mark.parametrize(("mode", "setter", "address"), GRID_POWERS)
async def test_reselecting_a_grid_mode_keeps_the_power_just_written(
    control, mode, setter, address
):
    """Reaudit RC-01: the power came from the last poll.

    An automation that selected its grid mode again before the next poll put
    the power back to what that poll had seen.
    """
    await control.set_mode(mode)
    await getattr(control, setter)(2560)

    await control.set_mode(mode)

    assert control._storage.modbus_unit.holding[address] == (-2500) & 0xFFFF


def _ignored_mode(_address):
    """The inverter kept AUTO as if the mode write never happened (upstream #127)."""
    return {STOR_CTL_MOD: 0, IN_W_RTE: 10000, OUT_W_RTE: 10000}


def _raised_by_another_controller(power_address):
    return {power_address: 10000}


def _auto_with_the_rates_left(_address):
    """A schedule switches to AUTO and leaves the grid power in its register."""
    return {STOR_CTL_MOD: 0}


@pytest.mark.parametrize("poll", [False, True])
@pytest.mark.parametrize(
    "left", [_ignored_mode, _raised_by_another_controller, _auto_with_the_rates_left]
)
@pytest.mark.parametrize(("mode", "setter", "address"), GRID_POWERS)
async def test_reselecting_a_grid_mode_over_a_non_grid_rate_restores_it(
    control, inverter_unit, mode, setter, address, left, poll
):
    """Reaudit D-02: "the same mode again" was judged by the mode last chosen.

    With a positive rate in the power register, only StorCtl_Mod went out and
    the image was Discharge Limit or PV Charge Limit instead of the grid mode.
    Judged by the power register alone, AUTO with the rates left brought the
    power of the earlier visit back (reaudit E-01).
    """
    await control.set_mode(mode)
    await getattr(control, setter)(2560)
    inverter_unit.holding.update(left(address))
    if poll:
        await control._storage.async_update()
        control.sync_from_device()

    await control.set_mode(mode)

    image = [inverter_unit.holding[a] for a in (STOR_CTL_MOD, IN_W_RTE, OUT_W_RTE)]
    expected = {OUT_W_RTE: [2, 10000, 0], IN_W_RTE: [1, 0, 10000]}[address]
    assert image == expected


@pytest.mark.parametrize(("mode", "setter", "address"), GRID_POWERS)
async def test_reselecting_a_grid_mode_keeps_the_power_when_it_cannot_be_read(
    control, inverter_unit, mode, setter, address
):
    """The device refuses a read for a moment right after a write.

    Judged by an unreadable register, the power just set went back to 0 W.
    """
    await control.set_mode(mode)
    await getattr(control, setter)(2560)
    inverter_unit.fail_read(address, ServerDeviceFailureError())

    await control.set_mode(mode)

    assert inverter_unit.holding[address] == (-2500) & 0xFFFF


async def _select_auto(control):
    await control.set_mode(ExtendedMode.AUTO)


async def _auto_from_the_device(control):
    """A schedule on the inverter switches to AUTO and leaves the rates as they are."""
    control._storage.modbus_unit.holding[STOR_CTL_MOD] = 0
    await control._storage.async_update()
    control.sync_from_device()


@pytest.mark.parametrize("leave", [_select_auto, _auto_from_the_device])
@pytest.mark.parametrize(("mode", "setter", "address"), GRID_POWERS)
async def test_entering_a_grid_mode_starts_at_zero_watts(
    control, mode, setter, address, leave
):
    """Reaudit RC-01: a power set in a mode left since came back.

    The README promises 0 W after the change; the last poll still held it.
    """
    await control.set_mode(mode)
    await getattr(control, setter)(2560)
    await control._storage.async_update()
    control.sync_from_device()
    await leave(control)

    await control.set_mode(mode)

    assert control._storage.modbus_unit.holding[address] == 0


@pytest.mark.parametrize(("mode", "setter", "address"), GRID_POWERS)
async def test_entering_a_grid_mode_another_controller_set_starts_at_zero_watts(
    control, inverter_unit, mode, setter, address
):
    """The image already fits the grid mode, but selecting it is still a change."""
    sunspec_mode = {OUT_W_RTE: 2, IN_W_RTE: 1}[address]
    inverter_unit.holding.update(
        {STOR_CTL_MOD: sunspec_mode, address: (-2500) & 0xFFFF}
    )

    await control.set_mode(mode)

    assert inverter_unit.holding[address] == 0


@pytest.mark.parametrize("mode", list(ExtendedMode))
async def test_a_mode_already_in_place_writes_nothing(control, writes, mode):
    """Picking the current mode again wrote StorCtl_Mod and both rates."""
    await control.set_mode(mode)
    del writes[:]

    await control.set_mode(mode)

    assert writes == []


async def test_a_mode_change_writes_only_the_registers_that_differ(control, writes):
    """Both limits and blocked discharging share the base mode; one rate moves."""
    await control.set_mode(ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT)
    del writes[:]

    await control.set_mode(ExtendedMode.BLOCK_DISCHARGING)

    assert [event.address for event in writes] == [OUT_W_RTE]


async def test_an_unchanged_rate_or_reserve_is_not_written(control, writes):
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(5120)
    await control.set_minimum_reserve(7)
    del writes[:]

    await control.set_charge_limit_w(5120)
    await control.set_minimum_reserve(7)

    assert writes == []


async def test_a_limit_is_refused_outside_its_mode(control):
    with pytest.raises(ValueError, match="Charge limit cannot be changed"):
        await control.set_charge_limit_w(5000)


async def test_a_discharge_limit_writes_the_discharge_rate_from_its_own_maximum(
    control, writes
):
    """Audit F25: no test set a discharge limit, so a write to InWRte passed."""
    control.max_discharge_rate_w = 5120
    await control.set_mode(ExtendedMode.DISCHARGE_LIMIT)
    del writes[:]

    await control.set_discharge_limit_w(2560)

    assert [(event.address, event.values[0]) for event in writes] == [(OUT_W_RTE, 5000)]


async def test_a_discharge_limit_cannot_overwrite_the_grid_charge_power(
    control, writes
):
    """Audit F25: in Charge from Grid the negative OutWRte is the grid charge power."""
    await control.set_mode(ExtendedMode.CHARGE_FROM_GRID)
    await control.set_grid_charge_power_w(2560)
    del writes[:]

    with pytest.raises(ValueError, match="Discharge limit cannot be changed"):
        await control.set_discharge_limit_w(5000)

    assert writes == []


# Every limit a mode leaves to the user, and the register of the other side.
ZERO_LIMITS = [
    (mode, setter, other)
    for setter, modes, other in (
        ("set_charge_limit_w", CHARGE_LIMIT_MODES, OUT_W_RTE),
        ("set_discharge_limit_w", DISCHARGE_LIMIT_MODES, IN_W_RTE),
    )
    for mode in sorted(modes)
]
BLOCK_MODES = (ExtendedMode.BLOCK_CHARGING, ExtendedMode.BLOCK_DISCHARGING)


@pytest.mark.parametrize("watts", [0, 0.1])
@pytest.mark.parametrize(("mode", "setter", "other"), ZERO_LIMITS)
async def test_a_zero_limit_that_blocks_both_ways_is_refused(
    control, writes, inverter_unit, mode, setter, other, watts
):
    """Reaudit W-02: both rates at 0 W is an image the firmware refuses.

    The write went out and came back as a bare Modbus error instead of a
    reason. The other side was read from the mode table, so in PV Charge and
    Discharge Limit a second 0 W limit still went out (reaudit D-01), and
    0.1 W passed the check to become a 0 in the register (reaudit AC-01).
    """
    await control.set_mode(mode)
    inverter_unit.holding[other] = 0
    del writes[:]

    with pytest.raises(ControlRefused) as refused:
        await getattr(control, setter)(watts)

    assert refused.value.key == "limit_blocks_both_ways"
    assert writes == []


@pytest.mark.parametrize(("mode", "setter", "other"), ZERO_LIMITS)
async def test_a_zero_limit_is_judged_by_the_mode_when_the_other_side_cannot_be_read(
    control, writes, inverter_unit, mode, setter, other
):
    """An unreadable other side falls back to what the mode holds there."""
    await control.set_mode(mode)
    inverter_unit.fail_read(other, ServerDeviceFailureError())
    del writes[:]
    blocked = mode in BLOCK_MODES
    outcome = pytest.raises(ControlRefused) if blocked else contextlib.nullcontext()

    with outcome:
        await getattr(control, setter)(0)

    assert len(writes) == (0 if blocked else 1)


@pytest.mark.parametrize(
    ("mode", "setter", "other"),
    [row for row in ZERO_LIMITS if row[0] not in BLOCK_MODES],
)
async def test_a_zero_limit_is_written_while_the_other_side_is_open(
    control, writes, inverter_unit, mode, setter, other
):
    """The refusal is for both sides at 0 W, not for a mode (reaudit D-01)."""
    await control.set_mode(mode)
    inverter_unit.holding[other] = 10000
    del writes[:]

    await getattr(control, setter)(0)

    assert [event.values[0] for event in writes] == [0]


# Every rate setter and a mode that allows it.
RATE_SETTERS = [(mode, setter) for mode, setter, _ in ZERO_LIMITS] + [
    (mode, setter) for mode, setter, _ in GRID_POWERS
]


@pytest.mark.parametrize(("mode", "setter"), RATE_SETTERS)
async def test_a_rate_is_refused_once_the_inverter_left_the_mode(
    control, writes, inverter_unit, mode, setter
):
    """Reaudit AC-01: a rate was judged by the mode last chosen.

    A schedule or another controller changed the mode between two polls; the
    rate went into that mode's image instead.
    """
    await control.set_mode(mode)
    inverter_unit.holding[STOR_CTL_MOD] = 0
    del writes[:]

    with pytest.raises(ControlRefused) as refused:
        await getattr(control, setter)(2560)

    assert refused.value.key == "storage_mode_left"
    assert writes == []


@pytest.mark.parametrize(("mode", "setter", "address"), GRID_POWERS)
async def test_a_grid_power_is_refused_over_both_limits_of_another_controller(
    control, writes, inverter_unit, mode, setter, address
):
    """Reaudit AC-01: StorCtl_Mod 3 with a negative rate beyond the other is a
    window the firmware refuses."""
    await control.set_mode(mode)
    inverter_unit.holding.update({STOR_CTL_MOD: 3, IN_W_RTE: 2500, OUT_W_RTE: 2500})
    del writes[:]

    with pytest.raises(ControlRefused) as refused:
        await getattr(control, setter)(5120)

    assert refused.value.key == "storage_mode_left"
    assert writes == []


async def test_a_rate_above_the_maximum_is_clamped_to_100_percent(
    control, writes, inverter_unit
):
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    inverter_unit.holding[IN_W_RTE] = 5000
    await control.set_charge_limit_w(99999)
    assert _words(writes, IN_W_RTE)[-1] == 10000


async def test_minimum_reserve_is_written_as_whole_percent(control, writes):
    await control.set_minimum_reserve(7)
    assert _words(writes, MIN_RSV_PCT) == [700]
    with pytest.raises(ValueError):
        await control.set_minimum_reserve(4)


async def test_blocking_discharge_still_allows_a_charge_limit(control, writes):
    """Blocking discharge leaves the charge path under control.

    The device is in mode 3 with the discharge rate at zero; the charge rate
    is still the user's to set, and refusing it here would strand the entity.
    """
    await control.set_mode(ExtendedMode.BLOCK_DISCHARGING)
    await control.set_charge_limit_w(5120)

    assert _words(writes, IN_W_RTE)[-1] == 5000


CHA_GRI_SET = STORAGE_HEADER + 17


async def test_a_negative_discharge_rate_reads_as_charging(inverter_unit):
    """The sensor names what the battery is doing, not which register was written.

    A negative discharge rate is how charging from the grid is expressed; showing
    the raw mode there would say "discharge" while the battery fills.
    """
    inverter_unit.holding[OUT_W_RTE] = -3000 & 0xFFFF
    chain = await scan(inverter_unit, 40000)
    storage = Storage(inverter_unit, chain.first(STORAGE_MODEL_ID))
    await storage.async_update()
    control = StorageControl(
        storage, max_charge_rate_w=10240, max_discharge_rate_w=10240
    )
    control.sync_from_device()

    assert control.control_mode == 1


async def test_charging_from_the_grid_reads_as_charging(control, inverter_unit):
    inverter_unit.holding[CHA_GRI_SET] = 1
    await control.set_mode(ExtendedMode.CHARGE_FROM_GRID)
    await control._storage.async_update()

    assert control.control_mode == 1


async def test_discharging_to_the_grid_reads_as_discharging(control):
    await control.set_mode(ExtendedMode.DISCHARGE_TO_GRID)
    await control._storage.async_update()

    assert control.control_mode == 2


async def test_a_write_succeeds_when_the_read_right_after_it_is_refused(
    control, inverter_unit, writes
):
    """The inverter refuses a read for a moment right after a write (real GEN24, fw 1.38.6-1).

    set_minimum_reserve wrote MinRsvPct successfully but then raised, because
    it read the storage block back inside the write lock and the device
    answered that read with a transient exception 4.
    """

    def arm_read_failure(_event):
        inverter_unit.fail_read(STORAGE_HEADER, ServerDeviceFailureError())

    inverter_unit.on_write(arm_read_failure)

    await control.set_minimum_reserve(6)

    assert _words(writes, MIN_RSV_PCT) == [600]


def _refuse_the_next_read(inverter_unit) -> None:
    """Answer the block read that follows the next write with exception 4."""
    inverter_unit.on_write(
        lambda _event: inverter_unit.fail_read(
            STORAGE_HEADER, ServerDeviceFailureError()
        )
    )


async def test_a_mode_write_succeeds_when_the_read_right_after_it_is_refused(
    control, inverter_unit, writes
):
    """Same transient refusal as the reserve write: the mode write must survive it."""
    _refuse_the_next_read(inverter_unit)

    await control.set_mode(ExtendedMode.CHARGE_FROM_GRID)

    assert _words(writes, STOR_CTL_MOD) == [2]
    assert control.extended_mode is ExtendedMode.CHARGE_FROM_GRID


async def test_a_rate_write_succeeds_when_the_read_right_after_it_is_refused(
    control, inverter_unit, writes
):
    """Same transient refusal as the reserve write: a rate write must survive it."""
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    _refuse_the_next_read(inverter_unit)

    await control.set_charge_limit_w(5120)

    assert _words(writes, IN_W_RTE)[-1] == 5000


@pytest.fixture
def no_comparison(monkeypatch):
    """For fakes without registers: the ordering is the subject, not the comparison."""

    async def nothing_holds(_component, _wanted):
        return set()

    monkeypatch.setattr(storage_module, "registers_holding", nothing_holds)


@pytest.mark.usefixtures("no_comparison")
async def test_a_rate_queued_behind_a_mode_switch_is_judged_by_the_new_mode():
    """Audit F02: validated before the lock, a charge limit undid a fresh charging block."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class FakeStorage:
        stor_ctl_mod = 1
        in_w_rte = 100.0
        out_w_rte = 100.0

        async def write(self, field, value):
            setattr(self, field, value)
            if field == "stor_ctl_mod":
                entered.set()
                await release.wait()

    storage = FakeStorage()
    control = StorageControl(
        storage, max_charge_rate_w=10000, max_discharge_rate_w=10000
    )
    control.sync_from_device()
    mode_task = asyncio.create_task(control.set_mode(ExtendedMode.BLOCK_CHARGING))
    await entered.wait()
    rate_task = asyncio.create_task(control.set_charge_limit_w(5000))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(mode_task, rate_task, return_exceptions=True)
    assert isinstance(results[1], ValueError)
    assert storage.in_w_rte == 0


def test_forced_discharge_is_recognised_at_startup():
    """Audit F08: mode 1 with a negative InWRte read as a PV charge limit."""
    storage = SimpleNamespace(stor_ctl_mod=1, in_w_rte=-50.0, out_w_rte=100.0)
    control = StorageControl(
        storage, max_charge_rate_w=10000, max_discharge_rate_w=10000
    )
    control.sync_from_device()
    assert control.extended_mode is ExtendedMode.DISCHARGE_TO_GRID


@pytest.mark.parametrize("mode", list(ExtendedMode))
async def test_every_written_mode_is_read_back_as_itself(inverter_unit, mode):
    chain = await scan(inverter_unit, 40000)
    storage = Storage(inverter_unit, chain.first(STORAGE_MODEL_ID))
    await storage.async_update()
    control = StorageControl(
        storage, max_charge_rate_w=10000, max_discharge_rate_w=10000
    )
    control.sync_from_device()
    await control.set_mode(mode)
    if mode is ExtendedMode.CHARGE_FROM_GRID:
        await control.set_grid_charge_power_w(2500)
    if mode is ExtendedMode.DISCHARGE_TO_GRID:
        await control.set_grid_discharge_power_w(2500)
    await storage.async_update()
    fresh = StorageControl(storage, max_charge_rate_w=10000, max_discharge_rate_w=10000)
    fresh.sync_from_device()
    assert fresh.extended_mode is mode


async def test_a_mode_changed_outside_home_assistant_is_adopted(control):
    """Audit F07 / upstream #127: the select kept showing the mode it had written."""
    await control.set_mode(ExtendedMode.BLOCK_CHARGING)
    await control._storage.async_update()
    control.sync_from_device()
    assert control.extended_mode is ExtendedMode.BLOCK_CHARGING
    control._storage.modbus_unit.holding[STOR_CTL_MOD] = 0
    await control._storage.async_update()
    control.sync_from_device()
    assert control.extended_mode is ExtendedMode.AUTO


async def test_a_mode_the_inverter_refuses_is_given_up_after_the_grace_polls(control):
    """Audit F07: a silently refused mode write was reported as done forever."""
    storage = control._storage
    written = []
    storage.modbus_unit.on_write(written.append)
    await control.set_mode(ExtendedMode.BLOCK_DISCHARGING)
    # The inverter keeps its registers as if the write never happened.
    for event in written:
        storage.modbus_unit.holding[event.address] = (
            0 if event.address == STOR_CTL_MOD else 10000
        )
    for _ in range(MODE_WRITE_GRACE_POLLS - 1):
        await storage.async_update()
        control.sync_from_device()
        assert control.extended_mode is ExtendedMode.BLOCK_DISCHARGING
    await storage.async_update()
    control.sync_from_device()
    assert control.extended_mode is ExtendedMode.AUTO


# A mode at 0 W whose registers read exactly like another mode, and the
# setter that brings the rate to 0 W (None: the mode is written at 0 W).
ZERO_WATT_LOOKALIKES = [
    (ExtendedMode.CHARGE_FROM_GRID, None),
    (ExtendedMode.DISCHARGE_TO_GRID, None),
    (ExtendedMode.PV_CHARGE_LIMIT, "set_charge_limit_w"),
    (ExtendedMode.DISCHARGE_LIMIT, "set_discharge_limit_w"),
    (ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT, "set_charge_limit_w"),
    (ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT, "set_discharge_limit_w"),
]


@pytest.mark.parametrize(("mode", "setter"), ZERO_WATT_LOOKALIKES)
async def test_a_mode_at_zero_watts_keeps_its_mode_across_polls(control, mode, setter):
    """At 0 W these registers derive to a block mode; the choice must survive.

    Audit F26: only Charge from Grid was covered, so Discharge to Grid and the
    limit modes could flip to a block mode after the grace polls unnoticed.
    """
    await control.set_mode(mode)
    if setter is not None:
        await getattr(control, setter)(0)
    for _ in range(MODE_WRITE_GRACE_POLLS + 1):
        await control._storage.async_update()
        control.sync_from_device()
    assert control.extended_mode is mode


# The register images evcc writes for its battery modes (fronius-gen24 template):
# the same extended modes on a different base mode than this integration writes.
EVCC_IMAGES = [
    ("hold", 2, 0, 10000, ExtendedMode.BLOCK_DISCHARGING),
    ("holdcharge", 1, 10000, 0, ExtendedMode.BLOCK_CHARGING),
    ("discharge", 3, 10000, -10000 & 0xFFFF, ExtendedMode.DISCHARGE_TO_GRID),
]


@pytest.mark.parametrize(
    ("label", "stor_ctl_mod", "out_w_rte", "in_w_rte", "expected"), EVCC_IMAGES
)
async def test_a_mode_another_controller_wrote_is_adopted_once_not_every_poll(
    control, caplog, label, stor_ctl_mod, out_w_rte, in_w_rte, expected
):
    """callifo #138: evcc's images map to our modes but not to the images we write.

    Judging them against our own write image made every poll an "outside change".
    """
    unit = control._storage.modbus_unit
    unit.holding[STOR_CTL_MOD] = stor_ctl_mod
    unit.holding[OUT_W_RTE] = out_w_rte
    unit.holding[IN_W_RTE] = in_w_rte
    await control._storage.async_update()
    control.sync_from_device()
    assert control.extended_mode is expected

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await control._storage.async_update()
        control.sync_from_device()
    assert control.extended_mode is expected
    assert [r.getMessage() for r in caplog.records] == []


# The setters each mode allows, and the register each one writes.
SETTERS_IN = {
    ExtendedMode.PV_CHARGE_LIMIT: [("set_charge_limit_w", IN_W_RTE)],
    ExtendedMode.DISCHARGE_LIMIT: [("set_discharge_limit_w", OUT_W_RTE)],
    ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT: [
        ("set_charge_limit_w", IN_W_RTE),
        ("set_discharge_limit_w", OUT_W_RTE),
    ],
    ExtendedMode.CHARGE_FROM_GRID: [("set_grid_charge_power_w", OUT_W_RTE)],
    ExtendedMode.DISCHARGE_TO_GRID: [("set_grid_discharge_power_w", IN_W_RTE)],
    ExtendedMode.BLOCK_DISCHARGING: [("set_charge_limit_w", IN_W_RTE)],
    ExtendedMode.BLOCK_CHARGING: [("set_discharge_limit_w", OUT_W_RTE)],
}
# Images another controller leaves, or a restart reads, that are no table image:
# evcc's battery modes, and the grid modes at 0 W that read as a block mode.
FOREIGN_FITTING_IMAGES = [
    (stor_ctl_mod, out_w_rte, in_w_rte)
    for _, stor_ctl_mod, out_w_rte, in_w_rte, _ in EVCC_IMAGES
] + [(2, 0, 10000), (1, 10000, 0)]


@pytest.mark.parametrize(
    ("stor_ctl_mod", "out_w_rte", "in_w_rte"), FOREIGN_FITTING_IMAGES
)
async def test_a_rate_over_an_image_that_fits_the_mode_is_written(
    inverter_unit, writes, stor_ctl_mod, out_w_rte, in_w_rte
):
    """Reaudit G-02: the refusal judges the whole image, not StorCtl_Mod alone.

    Compared with the mode's own table image, every rate under evcc or after a
    restart was refused although the image is that mode.
    """
    inverter_unit.holding.update(
        {STOR_CTL_MOD: stor_ctl_mod, OUT_W_RTE: out_w_rte, IN_W_RTE: in_w_rte}
    )
    chain = await scan(inverter_unit, 40000)
    storage = Storage(inverter_unit, chain.first(STORAGE_MODEL_ID))
    await storage.async_update()
    control = StorageControl(
        storage, max_charge_rate_w=10240, max_discharge_rate_w=10240
    )
    control.sync_from_device()

    for setter, address in SETTERS_IN[control.extended_mode]:
        del writes[:]
        await getattr(control, setter)(2560)
        assert [event.address for event in writes] == [address]


# A mode, and the image of another mode on the same StorCtl_Mod.
SAME_BASE_FOREIGN_IMAGES = [
    (ExtendedMode.DISCHARGE_LIMIT, {OUT_W_RTE: (-5000) & 0xFFFF}),
    (ExtendedMode.PV_CHARGE_LIMIT, {IN_W_RTE: (-5000) & 0xFFFF}),
    (ExtendedMode.CHARGE_FROM_GRID, {OUT_W_RTE: 10000}),
    (ExtendedMode.DISCHARGE_TO_GRID, {IN_W_RTE: 10000}),
]


@pytest.mark.parametrize(("mode", "image"), SAME_BASE_FOREIGN_IMAGES)
async def test_a_rate_over_another_mode_on_the_same_base_is_refused(
    control, writes, inverter_unit, mode, image
):
    """Reaudit G-02: a schedule left StorCtl_Mod as it was and changed a rate.

    Judged by StorCtl_Mod alone, a discharge limit replaced the grid charging
    the inverter was doing.
    """
    await control.set_mode(mode)
    inverter_unit.holding.update(image)
    setter, _ = SETTERS_IN[mode][0]
    del writes[:]

    with pytest.raises(ControlRefused) as refused:
        await getattr(control, setter)(2560)

    assert refused.value.key == "storage_mode_left"
    assert writes == []


IN_OUT_W_RTE_SF = STORAGE_HEADER + 25


@pytest.mark.parametrize(
    ("mode", "word"),
    [
        (ExtendedMode.PV_CHARGE_LIMIT, 2500),
        (ExtendedMode.DISCHARGE_LIMIT, 2500),
        (ExtendedMode.CHARGE_FROM_GRID, (-2500) & 0xFFFF),
        (ExtendedMode.DISCHARGE_TO_GRID, (-2500) & 0xFFFF),
    ],
)
async def test_a_rate_is_written_as_asked_when_its_scale_is_refused_once(
    control, writes, inverter_unit, monkeypatch, mode, word
):
    """Reaudit G-03: the first read right after a write may be refused.

    The rate is then judged as asked, not as the register would round it,
    and written all the same.
    """
    await control.set_mode(mode)
    read = inverter_unit.read_holding_registers
    refused = []

    async def refuse_the_first_scale_read(address, count):
        if address == IN_OUT_W_RTE_SF and not refused:
            refused.append(address)
            raise ServerDeviceFailureError
        return await read(address, count)

    monkeypatch.setattr(
        inverter_unit, "read_holding_registers", refuse_the_first_scale_read
    )
    setter, address = SETTERS_IN[mode][0]
    del writes[:]

    await getattr(control, setter)(2560)

    assert refused
    assert _words(writes, address) == [word]
