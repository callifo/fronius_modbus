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
        # Discharge to Grid is StorCtl_Mod 1: on 2 the device ignores InWRte.
        (1, 10000, -3000 & 0xFFFF, ExtendedMode.DISCHARGE_TO_GRID),
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
# The modes this integration writes with both rates switched on (StorCtl_Mod 3).
BOTH_RATES_ON = {
    ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT,
    ExtendedMode.BLOCK_CHARGING,
    ExtendedMode.BLOCK_DISCHARGING,
}
ZERO_LIMITS_BOTH_ON = [row for row in ZERO_LIMITS if row[0] in BOTH_RATES_ON]
ZERO_LIMITS_OTHER_OFF = [row for row in ZERO_LIMITS if row[0] not in BOTH_RATES_ON]


@pytest.mark.parametrize("watts", [0, 0.1])
@pytest.mark.parametrize(("mode", "setter", "other"), ZERO_LIMITS_BOTH_ON)
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
    # Home Assistant's own 0 on the other side; written past it, it is another
    # controller's, and the image no longer the selected mode's.
    other_setter = (
        "set_discharge_limit_w"
        if setter == "set_charge_limit_w"
        else "set_charge_limit_w"
    )
    if mode is ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT:
        await getattr(control, other_setter)(0)
    else:
        inverter_unit.holding[other] = 0
    del writes[:]

    with pytest.raises(ControlRefused) as refused:
        await getattr(control, setter)(watts)

    assert refused.value.key == "limit_blocks_both_ways"
    assert writes == []


@pytest.mark.parametrize(("mode", "setter", "other"), ZERO_LIMITS_OTHER_OFF)
async def test_a_zero_limit_beside_a_rate_that_is_off_is_written(
    control, writes, inverter_unit, mode, setter, other
):
    """StorCtl_Mod 1 and 2 switch the other rate off, and the device takes (1, 0, 0).

    Refused as "both ways blocked", a 0 W PV charge or discharge limit could
    not be set after another controller had left the other rate at 0.
    """
    await control.set_mode(mode)
    inverter_unit.holding[other] = 0
    del writes[:]

    await getattr(control, setter)(0)

    assert [event.address for event in writes] == [
        IN_W_RTE if setter == "set_charge_limit_w" else OUT_W_RTE
    ]


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

    async def as_given(_component, _name, value):
        return value

    monkeypatch.setattr(storage_module, "registers_holding", nothing_holds)
    monkeypatch.setattr(storage_module, "written_value", as_given)


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
    # A mode switch that fails before its first write must fail the test, not hang it.
    await asyncio.wait_for(entered.wait(), timeout=5)
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


async def _control_on(inverter_unit, image):
    """A control that starts on the given registers, as after a restart."""
    inverter_unit.holding.update(image)
    chain = await scan(inverter_unit, 40000)
    storage = Storage(inverter_unit, chain.first(STORAGE_MODEL_ID))
    await storage.async_update()
    control = StorageControl(
        storage, max_charge_rate_w=10240, max_discharge_rate_w=10240
    )
    control.sync_from_device()
    return control


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
    control = await _control_on(
        inverter_unit,
        {STOR_CTL_MOD: stor_ctl_mod, OUT_W_RTE: out_w_rte, IN_W_RTE: in_w_rte},
    )

    for setter, address in SETTERS_IN[control.extended_mode]:
        del writes[:]
        await getattr(control, setter)(2560)
        switched_on = _rate_on(stor_ctl_mod, address)
        assert [event.address for event in writes] == (
            [address] if switched_on else [address, STOR_CTL_MOD]
        )


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


# A grid mode another controller holds on StorCtl_Mod 3, the other rate at 25 %.
COMBINED_GRID_IMAGES = [
    (
        ExtendedMode.CHARGE_FROM_GRID,
        "set_grid_charge_power_w",
        OUT_W_RTE,
        {STOR_CTL_MOD: 3, IN_W_RTE: 2500, OUT_W_RTE: (-2000) & 0xFFFF},
    ),
    (
        ExtendedMode.DISCHARGE_TO_GRID,
        "set_grid_discharge_power_w",
        IN_W_RTE,
        {STOR_CTL_MOD: 3, IN_W_RTE: (-2000) & 0xFFFF, OUT_W_RTE: 2500},
    ),
]


@pytest.mark.parametrize(("mode", "setter", "address", "image"), COMBINED_GRID_IMAGES)
async def test_a_grid_power_beyond_a_combined_window_is_refused(
    inverter_unit, writes, mode, setter, address, image
):
    """Reaudit CX-01: on StorCtl_Mod 3 the firmware refuses a negative rate
    beyond the other one; the power went out and came back as a bare error."""
    control = await _control_on(inverter_unit, image)
    assert control.extended_mode is mode
    del writes[:]

    with pytest.raises(ControlRefused) as refused:
        await getattr(control, setter)(5120)

    assert refused.value.key == "grid_power_beyond_window"
    assert writes == []


@pytest.mark.parametrize(("mode", "setter", "address", "image"), COMBINED_GRID_IMAGES)
async def test_a_grid_power_inside_a_combined_window_is_written(
    inverter_unit, writes, mode, setter, address, image
):
    """The window's own bound is no refusal: -25 % against 25 % is legal."""
    control = await _control_on(inverter_unit, image)
    del writes[:]

    await getattr(control, setter)(2560)

    assert _words(writes, address) == [(-2500) & 0xFFFF]


def _rate_on(stor_ctl_mod: int, address: int) -> bool:
    """Whether StorCtl_Mod switches the rate at ``address`` on (bit 0 InWRte, bit 1 OutWRte)."""
    return bool(stor_ctl_mod & (1 if address == IN_W_RTE else 2))


def _raw(percent: float) -> int:
    return round(percent * 100) & 0xFFFF


# (StorCtl_Mod, InWRte %, OutWRte %) measured on a GEN24 (fw 1.42.9-1) with an
# outside writer, and their mirrors: the rate StorCtl_Mod keeps off is ignored.
OFF_RATE_IMAGES = [
    ((2, -10, 0), ExtendedMode.BLOCK_DISCHARGING),
    ((1, 0, -10), ExtendedMode.BLOCK_CHARGING),
    ((2, -100, 0), ExtendedMode.BLOCK_DISCHARGING),
    ((1, 0, -50), ExtendedMode.BLOCK_CHARGING),
    ((1, 50, -10), ExtendedMode.PV_CHARGE_LIMIT),
    ((2, -50, 50), ExtendedMode.DISCHARGE_LIMIT),
    ((1, -20, -30), ExtendedMode.DISCHARGE_TO_GRID),
    ((2, -20, -30), ExtendedMode.CHARGE_FROM_GRID),
    ((1, 0, 0), ExtendedMode.BLOCK_CHARGING),
    ((2, 0, 0), ExtendedMode.BLOCK_DISCHARGING),
]


@pytest.mark.parametrize(("image", "expected"), OFF_RATE_IMAGES)
async def test_a_rate_storctl_mod_keeps_off_does_not_decide_the_mode(
    inverter_unit, image, expected
):
    """evcc's hold after discharge read as Discharge to Grid, holdcharge after charge as Charge from Grid.

    StorCtl_Mod is a bitfield: bit 0 switches InWRte on, bit 1 OutWRte. The
    device ignores the other rate (measured), and evcc leaves it as it was.
    """
    stor_ctl_mod, in_rate, out_rate = image
    control = await _control_on(
        inverter_unit,
        {
            STOR_CTL_MOD: stor_ctl_mod,
            IN_W_RTE: _raw(in_rate),
            OUT_W_RTE: _raw(out_rate),
        },
    )

    assert control.extended_mode is expected


@pytest.mark.parametrize(("image", "expected"), OFF_RATE_IMAGES)
async def test_a_rate_that_is_off_shows_as_no_limit(inverter_unit, image, expected):
    """A limit or grid power the device ignores is no limit; it showed the stale register."""
    stor_ctl_mod, in_rate, out_rate = image
    control = await _control_on(
        inverter_unit,
        {
            STOR_CTL_MOD: stor_ctl_mod,
            IN_W_RTE: _raw(in_rate),
            OUT_W_RTE: _raw(out_rate),
        },
    )

    if not stor_ctl_mod & 1:
        assert (control.charge_limit_pct, control.grid_discharge_power_pct) == (
            100.0,
            0.0,
        )
    if not stor_ctl_mod & 2:
        assert (control.discharge_limit_pct, control.grid_charge_power_pct) == (
            100.0,
            0.0,
        )


# evcc's battery modes (fronius-gen24 template, evcc 25a8e25): the registers it
# writes, in order. The rate it leaves out keeps whatever the last writer left.
EVCC_SEQUENCES = {
    "normal": [(STOR_CTL_MOD, 0), (OUT_W_RTE, 100), (IN_W_RTE, 100)],
    "hold": [(STOR_CTL_MOD, 2), (OUT_W_RTE, 0)],
    "charge": [(STOR_CTL_MOD, 2), (OUT_W_RTE, -50)],
    "holdcharge": [(STOR_CTL_MOD, 1), (IN_W_RTE, 0)],
    "discharge": [
        (STOR_CTL_MOD, 0),
        (IN_W_RTE, -100),
        (OUT_W_RTE, 100),
        (STOR_CTL_MOD, 3),
    ],
}
EVCC_MEANS = {
    "normal": ExtendedMode.AUTO,
    "hold": ExtendedMode.BLOCK_DISCHARGING,
    "charge": ExtendedMode.CHARGE_FROM_GRID,
    "holdcharge": ExtendedMode.BLOCK_CHARGING,
    "discharge": ExtendedMode.DISCHARGE_TO_GRID,
}


def _after(image: tuple[int, float, float], sequence) -> tuple[int, float, float]:
    registers = {STOR_CTL_MOD: image[0], IN_W_RTE: image[1], OUT_W_RTE: image[2]}
    registers.update(sequence)
    return registers[STOR_CTL_MOD], registers[IN_W_RTE], registers[OUT_W_RTE]


# Where evcc starts from: where each of its own modes leaves the registers,
# and every image this integration writes for a mode.
EVCC_STARTS = {
    **{
        f"evcc {name}": _after((0, 100, 100), sequence)
        for name, sequence in EVCC_SEQUENCES.items()
    },
    **{f"ha {mode.name}": storage_module._MODE_TABLE[mode] for mode in ExtendedMode},
}


@pytest.mark.parametrize("start", sorted(EVCC_STARTS))
@pytest.mark.parametrize("evcc_mode", sorted(EVCC_SEQUENCES))
async def test_every_evcc_mode_reads_as_its_mode_whatever_came_before(
    inverter_unit, start, evcc_mode
):
    """evcc writes one rate per mode, so the other is what the last writer left.

    Judged by both, the mode depended on the one before: hold after discharge
    read as Discharge to Grid.
    """
    stor_ctl_mod, in_rate, out_rate = _after(
        EVCC_STARTS[start], EVCC_SEQUENCES[evcc_mode]
    )
    control = await _control_on(
        inverter_unit,
        {
            STOR_CTL_MOD: stor_ctl_mod,
            IN_W_RTE: _raw(in_rate),
            OUT_W_RTE: _raw(out_rate),
        },
    )

    assert control.extended_mode is EVCC_MEANS[evcc_mode]


# A block mode another controller holds with one rate only, the limit the
# integration leaves to the user there, and the rate that limit is written to.
ONE_RATE_BLOCK_MODES = [
    ((1, 0, 100), "set_discharge_limit_w", OUT_W_RTE),
    ((2, 100, 0), "set_charge_limit_w", IN_W_RTE),
]


@pytest.mark.parametrize(("image", "setter", "address"), ONE_RATE_BLOCK_MODES)
async def test_a_limit_written_into_a_rate_that_is_off_switches_it_on(
    inverter_unit, writes, image, setter, address
):
    """Under evcc's hold or holdcharge the limit went into a register the device ignores."""
    stor_ctl_mod, in_rate, out_rate = image
    control = await _control_on(
        inverter_unit,
        {
            STOR_CTL_MOD: stor_ctl_mod,
            IN_W_RTE: _raw(in_rate),
            OUT_W_RTE: _raw(out_rate),
        },
    )
    mode = control.extended_mode
    del writes[:]

    await getattr(control, setter)(5120)

    assert [event.address for event in writes] == [address, STOR_CTL_MOD]
    assert _words(writes, STOR_CTL_MOD) == [3]
    await control._storage.async_update()
    control.sync_from_device()
    assert control.extended_mode is mode


# A mode with a rate left to the user, its setter, the register, and the mode
# its image reads as when that rate is 0: the image both modes share.
SHARED_AT_ZERO = [
    (
        ExtendedMode.PV_CHARGE_LIMIT,
        "set_charge_limit_w",
        IN_W_RTE,
        ExtendedMode.BLOCK_CHARGING,
    ),
    (
        ExtendedMode.DISCHARGE_TO_GRID,
        "set_grid_discharge_power_w",
        IN_W_RTE,
        ExtendedMode.BLOCK_CHARGING,
    ),
    (
        ExtendedMode.DISCHARGE_LIMIT,
        "set_discharge_limit_w",
        OUT_W_RTE,
        ExtendedMode.BLOCK_DISCHARGING,
    ),
    (
        ExtendedMode.CHARGE_FROM_GRID,
        "set_grid_charge_power_w",
        OUT_W_RTE,
        ExtendedMode.BLOCK_DISCHARGING,
    ),
    (
        ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT,
        "set_charge_limit_w",
        IN_W_RTE,
        ExtendedMode.BLOCK_CHARGING,
    ),
    (
        ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT,
        "set_discharge_limit_w",
        OUT_W_RTE,
        ExtendedMode.BLOCK_DISCHARGING,
    ),
]


async def _poll(control) -> None:
    await control._storage.async_update()
    control.sync_from_device()


@pytest.mark.parametrize(("mode", "setter", "address", "shared"), SHARED_AT_ZERO)
async def test_a_rate_another_controller_sets_to_zero_leaves_the_mode(
    control, inverter_unit, caplog, mode, setter, address, shared
):
    """callifo 141: evcc's hold charge after a 2500 W PV charge limit kept showing the limit.

    At 0 the image is both modes; Home Assistant had written another rate, so
    someone else wrote this one.
    """
    await control.set_mode(mode)
    await getattr(control, setter)(2560)
    await _poll(control)
    inverter_unit.holding[address] = 0

    await _poll(control)

    assert control.extended_mode is shared
    assert "Storage mode changed outside Home Assistant" in caplog.text


@pytest.mark.parametrize(("mode", "setter", "address", "shared"), SHARED_AT_ZERO)
async def test_a_rate_home_assistant_set_to_zero_keeps_the_mode(
    control, mode, setter, address, shared
):
    """Home Assistant's own 0 W reads like the other mode; the choice must survive."""
    await control.set_mode(mode)
    await getattr(control, setter)(0)

    for _ in range(MODE_WRITE_GRACE_POLLS + 1):
        await _poll(control)

    assert control.extended_mode is mode


async def test_a_mode_change_half_written_is_not_taken_for_another_controller(
    control, inverter_unit
):
    """A poll between StorCtl_Mod and the rate of a mode change sees the image of the old mode."""
    await control.set_mode(ExtendedMode.BLOCK_CHARGING)
    await _poll(control)
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    inverter_unit.holding[IN_W_RTE] = 0

    await _poll(control)
    assert control.extended_mode is ExtendedMode.PV_CHARGE_LIMIT
    inverter_unit.holding[IN_W_RTE] = 10000
    await _poll(control)
    assert control.extended_mode is ExtendedMode.PV_CHARGE_LIMIT


async def test_after_a_restart_a_rate_set_to_zero_leaves_the_mode(inverter_unit):
    """Nothing written by Home Assistant since the restart: the registers decide."""
    control = await _control_on(
        inverter_unit, {STOR_CTL_MOD: 1, IN_W_RTE: 5000, OUT_W_RTE: 10000}
    )
    assert control.extended_mode is ExtendedMode.PV_CHARGE_LIMIT
    inverter_unit.holding[IN_W_RTE] = 0

    await _poll(control)

    assert control.extended_mode is ExtendedMode.BLOCK_CHARGING


async def test_a_rate_another_controller_changes_keeps_the_mode_until_it_is_zero(
    control, inverter_unit
):
    """1500 W instead of 2500 W is still a PV charge limit; 0 then is evcc's hold."""
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(2560)
    await _poll(control)
    inverter_unit.holding[IN_W_RTE] = 1500

    await _poll(control)
    assert control.extended_mode is ExtendedMode.PV_CHARGE_LIMIT
    inverter_unit.holding[IN_W_RTE] = 0
    await _poll(control)
    assert control.extended_mode is ExtendedMode.BLOCK_CHARGING


async def test_a_limit_is_refused_after_another_controller_took_the_shared_image(
    control, inverter_unit, writes
):
    """Written before the next poll noticed, the limit replaced evcc's hold charge."""
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(2560)
    await _poll(control)
    inverter_unit.holding[IN_W_RTE] = 0
    del writes[:]

    with pytest.raises(ControlRefused) as refused:
        await control.set_charge_limit_w(1000)

    assert refused.value.key == "storage_mode_left"
    assert writes == []


async def test_a_grid_power_kept_at_reselection_counts_as_home_assistant_s(
    control, inverter_unit
):
    """Selecting Discharge to Grid again keeps the power another controller set.

    Taken over as the mode's own, a later 0 from outside is outside again.
    """
    await control.set_mode(ExtendedMode.DISCHARGE_TO_GRID)
    await _poll(control)
    inverter_unit.holding[IN_W_RTE] = (-2000) & 0xFFFF
    await _poll(control)
    await control.set_mode(ExtendedMode.DISCHARGE_TO_GRID)
    await _poll(control)
    inverter_unit.holding[IN_W_RTE] = 0

    await _poll(control)

    assert control.extended_mode is ExtendedMode.BLOCK_CHARGING


async def test_a_poll_read_before_a_limit_write_keeps_the_mode(control):
    """The poll read the old 0 W, the limit went out, then the poll was judged.

    Against the limit just written, Home Assistant's own image looked foreign.
    """
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(0)
    await _poll(control)
    await control._storage.async_update()
    await control.set_charge_limit_w(2560)

    control.sync_from_device()
    assert control.extended_mode is ExtendedMode.PV_CHARGE_LIMIT
    await _poll(control)
    assert control.extended_mode is ExtendedMode.PV_CHARGE_LIMIT


async def test_a_failed_mode_write_leaves_the_record_of_the_last_one(
    control, inverter_unit
):
    """A mode change that failed half way must not count as written by Home Assistant."""
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
    await control.set_charge_limit_w(2560)
    await _poll(control)
    inverter_unit.fail_write(IN_W_RTE, ServerDeviceFailureError())
    with pytest.raises(ServerDeviceFailureError):
        await control.set_mode(ExtendedMode.DISCHARGE_TO_GRID)
    inverter_unit.fail_write(IN_W_RTE, None)
    inverter_unit.holding[IN_W_RTE] = 0

    await _poll(control)

    assert control.extended_mode is ExtendedMode.BLOCK_CHARGING


async def test_a_mode_taken_from_the_registers_owns_no_rate_of_home_assistant(
    control, inverter_unit
):
    """Discharge to Grid at 0 W, then evcc's PV charge limit, then its hold charge.

    Home Assistant's 0 W from before matched evcc's hold charge, so the PV
    charge limit evcc had left stayed on show.
    """
    await control.set_mode(ExtendedMode.DISCHARGE_TO_GRID)
    await _poll(control)
    inverter_unit.holding[IN_W_RTE] = 5000
    await _poll(control)
    assert control.extended_mode is ExtendedMode.PV_CHARGE_LIMIT
    inverter_unit.holding[IN_W_RTE] = 0

    await _poll(control)

    assert control.extended_mode is ExtendedMode.BLOCK_CHARGING


async def test_discharge_to_grid_selected_again_at_zero_watts_keeps_its_mode(control):
    """Selecting it again keeps the 0 W it holds; that 0 W is still Home Assistant's own."""
    await control.set_mode(ExtendedMode.DISCHARGE_TO_GRID)
    await _poll(control)
    await control.set_mode(ExtendedMode.DISCHARGE_TO_GRID)

    for _ in range(MODE_WRITE_GRACE_POLLS + 1):
        await _poll(control)

    assert control.extended_mode is ExtendedMode.DISCHARGE_TO_GRID


# A limit mode, its setter and register, and each await a rate write passes
# before its first write: a poll may run in any of them.
LIMIT_LOSES_ITS_MODE = [
    (ExtendedMode.PV_CHARGE_LIMIT, "set_charge_limit_w", IN_W_RTE),
    (ExtendedMode.DISCHARGE_LIMIT, "set_discharge_limit_w", OUT_W_RTE),
]
PRE_WRITE_AWAITS = ["written_value", "registers_holding"]


@pytest.mark.parametrize("stage", PRE_WRITE_AWAITS)
@pytest.mark.parametrize(("mode", "setter", "address"), LIMIT_LOSES_ITS_MODE)
async def test_a_limit_admitted_before_a_poll_took_another_mode_writes_nothing(
    control, inverter_unit, writes, monkeypatch, mode, setter, address, stage
):
    """Admitted in the limit mode, the write went out after a poll had taken evcc's hold.

    Judged by the mode selected at that moment, the image was the new block
    mode's, which allows no such limit.
    """
    await control.set_mode(mode)
    await getattr(control, setter)(2560)
    await _poll(control)
    real = getattr(storage_module, stage)

    async def poll_first(*args):
        # evcc's hold lands and a poll takes it while the write waits here.
        monkeypatch.setattr(storage_module, stage, real)
        inverter_unit.holding[address] = 0
        await _poll(control)
        return await real(*args)

    monkeypatch.setattr(storage_module, stage, poll_first)
    del writes[:]

    with pytest.raises(ControlRefused) as refused:
        await getattr(control, setter)(1000)

    assert refused.value.key == "storage_mode_left"
    assert writes == []
