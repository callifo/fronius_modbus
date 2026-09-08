"""AC limit and power factor writes with the enable pulse the inverter needs to apply a new value."""

import asyncio

from modbus_connection import ServerDeviceFailureError
from modbus_connection.model.sunspec import scan
import pytest

from custom_components.fronius_modbus.fronius_modbus_api.controls import (
    APPLY_MASK_SECONDS,
    InverterControls,
)
from custom_components.fronius_modbus.fronius_modbus_api.exceptions import (
    ControlLeftDisabledError,
)
from custom_components.fronius_modbus.fronius_modbus_api.sunspec_models import (
    CONTROLS_MODEL_ID,
    Controls,
)

CONTROLS_HEADER = 40227
CONN = CONTROLS_HEADER + 4
W_MAX_LIM_PCT = CONTROLS_HEADER + 5
W_MAX_LIM_ENA = CONTROLS_HEADER + 9
OUT_PF_SET = CONTROLS_HEADER + 10
OUT_PF_SET_ENA = CONTROLS_HEADER + 14


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def controls(inverter_unit, clock):
    chain = await scan(inverter_unit, 40000)
    component = Controls(inverter_unit, chain.first(CONTROLS_MODEL_ID))
    await component.async_update()
    return InverterControls(
        component, max_power_w=10000, monotonic=clock.monotonic, sleep=clock.sleep
    )


@pytest.fixture
def writes(inverter_unit):
    events = []
    inverter_unit.on_write(events.append)
    return events


def _at(events, address):
    return [(i, e.values[0]) for i, e in enumerate(events) if e.address == address]


async def test_ac_limit_in_watts_becomes_percent_of_max_power(controls, writes):
    await controls.set_ac_limit_w(2500)
    assert [w for _, w in _at(writes, W_MAX_LIM_PCT)] == [2500]  # 25.00 % with SF -2
    await controls._controls.async_update()
    assert controls.ac_limit_w == 2500


async def test_an_enabled_limit_is_pulsed_off_and_on_around_the_write(
    controls, writes, inverter_unit, clock
):
    inverter_unit.holding[W_MAX_LIM_ENA] = 1
    await controls.set_ac_limit_w(5000)
    order = sorted(_at(writes, W_MAX_LIM_ENA) + _at(writes, W_MAX_LIM_PCT))
    assert [w for _, w in order] == [0, 5000, 1]
    assert clock.slept == [1.0]
    assert controls.ac_limit_enabled is True
    clock.now += APPLY_MASK_SECONDS + 1
    inverter_unit.holding[W_MAX_LIM_ENA] = 0
    await controls._controls.async_update()
    assert controls.ac_limit_enabled is False


async def test_a_power_factor_outside_the_unit_range_is_refused(controls):
    with pytest.raises(ValueError):
        await controls.set_power_factor(1.5)


async def test_power_factor_is_written_scaled(controls, writes):
    await controls.set_power_factor(-0.95)
    assert [w for _, w in _at(writes, OUT_PF_SET)] == [(-950) & 0xFFFF]


async def test_connection_control(controls, writes):
    await controls.set_connected(False)
    assert [w for _, w in _at(writes, CONN)] == [0]


async def test_the_pulse_succeeds_when_the_read_right_after_it_is_refused(
    controls, inverter_unit, writes
):
    """The inverter refuses a read for a moment right after a write (real GEN24, fw 1.38.6-1).

    A write followed immediately by a read-back can hit a transient exception
    4 on the real device; the write itself must still count as a success.
    """

    def arm_read_failure(_event):
        inverter_unit.fail_read(CONTROLS_HEADER, ServerDeviceFailureError())

    inverter_unit.on_write(arm_read_failure)

    await controls.set_ac_limit_w(2500)

    assert [w for _, w in _at(writes, W_MAX_LIM_PCT)] == [2500]


async def test_a_failed_limit_write_leaves_the_limit_enabled(clock):
    """Audit F01: the pulse switched the limit off and never back on when the value write failed."""

    class FakeControls:
        w_max_lim_ena = 1
        w_max_lim_pct = 25.0
        writes = []

        async def async_update(self):
            pass

        async def write(self, field, value):
            self.writes.append((field, value))
            if field == "w_max_lim_pct":
                raise ServerDeviceFailureError
            setattr(self, field, value)

    component = FakeControls()
    controls = InverterControls(
        component, max_power_w=10000, monotonic=clock.monotonic, sleep=clock.sleep
    )
    with pytest.raises(ServerDeviceFailureError):
        await controls.set_ac_limit_w(5000)
    assert component.w_max_lim_ena == 1
    assert component.writes[-1] == ("w_max_lim_ena", 1)


async def test_a_limit_that_cannot_be_re_enabled_is_reported_as_left_disabled(clock):
    class FakeControls:
        w_max_lim_ena = 1

        async def async_update(self):
            pass

        async def write(self, field, value):
            if field == "w_max_lim_ena" and value == 0:
                setattr(self, field, value)
                return
            raise ServerDeviceFailureError

    component = FakeControls()
    controls = InverterControls(
        component, max_power_w=10000, monotonic=clock.monotonic, sleep=clock.sleep
    )
    with pytest.raises(ControlLeftDisabledError):
        await controls.set_ac_limit_w(5000)


@pytest.mark.parametrize(
    ("enable_address", "setter", "value"),
    [
        (W_MAX_LIM_ENA, "set_ac_limit_w", 5000.0),
        (OUT_PF_SET_ENA, "set_power_factor", 0.9),
    ],
)
async def test_a_cancelled_write_leaves_the_control_enabled(
    controls, inverter_unit, enable_address, setter, value
):
    """Audit A01: cancelling during the pulse left a live limit switched off for good."""
    inverter_unit.holding[enable_address] = 1
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_sleep(_seconds):
        entered.set()
        await release.wait()

    controls._sleep = blocking_sleep
    write = asyncio.create_task(getattr(controls, setter)(value))
    await entered.wait()
    write.cancel()
    with pytest.raises(asyncio.CancelledError):
        await write
    assert inverter_unit.holding[enable_address] == 1
