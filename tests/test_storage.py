"""The storage mode automaton and its write sequences, watched through the mock's write events."""

import asyncio
from types import SimpleNamespace

from modbus_connection import ServerDeviceFailureError
from modbus_connection.model.sunspec import scan
import pytest

from custom_components.fronius_modbus.fronius_modbus_api.storage import (
    MODE_WRITE_GRACE_POLLS,
    ExtendedMode,
    StorageControl,
)
from custom_components.fronius_modbus.fronius_modbus_api.sunspec_models import (
    STORAGE_MODEL_ID,
    Storage,
)

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


async def test_charge_from_grid_writes_mode_then_rates(control, writes):
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


async def test_grid_charge_power_is_a_negative_discharge_rate(control, writes):
    await control.set_mode(ExtendedMode.CHARGE_FROM_GRID)
    await control.set_grid_charge_power_w(2560)
    assert _words(writes, OUT_W_RTE)[-1] == (-2500) & 0xFFFF
    await control._storage.async_update()
    assert control.grid_charge_power_pct == 25.0


async def test_a_limit_is_refused_outside_its_mode(control):
    with pytest.raises(ValueError, match="Charge limit cannot be changed"):
        await control.set_charge_limit_w(5000)


async def test_a_rate_above_the_maximum_is_clamped_to_100_percent(control, writes):
    await control.set_mode(ExtendedMode.PV_CHARGE_LIMIT)
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


async def test_charge_from_grid_at_zero_watts_keeps_its_mode_across_polls(control):
    """The registers of Charge from Grid at 0 W equal Block Discharging; the choice must survive."""
    await control.set_mode(ExtendedMode.CHARGE_FROM_GRID)
    for _ in range(MODE_WRITE_GRACE_POLLS + 1):
        await control._storage.async_update()
        control.sync_from_device()
    assert control.extended_mode is ExtendedMode.CHARGE_FROM_GRID
