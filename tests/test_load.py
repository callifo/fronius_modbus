"""Regression tests for primary-meter load calculation and real polling paths."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from conftest import hub_module, client_module


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(hub_module.time, "monotonic", lambda: clock.now)
    return clock


@pytest_asyncio.fixture
async def hub(clock):
    hub = hub_module.Hub(None, "Test", "127.0.0.1", 502, 1, [200], 30)
    yield hub
    hub._client.close()


def sample(hub, *, ac=4000.0, meter=-3000.0, location=0,
           inverter_window=(99.8, 99.9), meter_window=(99.9, 100.0)):
    client = hub._client
    client.meter_configured = True
    client.data.update(acpower=ac, meter_200_power=meter, meter_200_location=location)
    client._load_inverter_request_start = inverter_window[0] if inverter_window else None
    client._load_inverter_sample_ts = inverter_window[1] if inverter_window else None
    client._load_meter_request_start = {200: meter_window[0]} if meter_window else {}
    client._load_meter_sample_ts = {200: meter_window[1]} if meter_window else {}


@pytest.mark.parametrize(("ac", "meter", "charge", "discharge", "expected"), [
    (4000, -3000, 0, 0, 1000),       # PV self-consumption
    (12000, -11800, 0, 0, 200),      # high export, low genuine load
    (0, 800, 0, 0, 800),            # grid import
    (1000, -400, 3000, 0, 600),      # PV charging DC battery
    (-3000, 3600, 3000, 0, 600),     # AC grid charging
    (1800, -1000, 0, 2000, 800),     # battery discharge
    (5602, -5572, 12.8, 0, 30),      # reported valid low-load example
    (5000, -5000, 0, 0, 0),         # measured exact zero
])
def test_power_flows(hub, ac, meter, charge, discharge, expected):
    sample(hub, ac=ac, meter=meter)
    hub._client.storage_configured = True
    hub.data.update(storage_charge_power=charge, storage_discharge_power=discharge)
    hub._apply_modbus_load_data()
    assert hub.data["load"] == expected


@pytest.mark.parametrize("windows", [
    ((99.8, 99.9), (99.9, 100.0)),
    ((99.5, 99.7), (99.9, 100.0)),  # inclusive 500-ms boundary
])
def test_small_request_window(hub, windows):
    sample(hub, inverter_window=windows[0], meter_window=windows[1])
    hub._apply_modbus_load_data()
    assert hub.data["load"] == 1000


@pytest.mark.parametrize(("inverter_window", "meter_window"), [
    ((99.0, 99.1), (99.9, 100.0)),  # large sample skew
    ((98.0, 99.99), (99.99, 100.0)), # long inverter response, close completions
    ((99.3, 99.4), (99.4, 100.0)),   # long meter response
    ((98.0, 98.1), (98.1, 98.2)),    # equally stale pair
    ((100.1, 100.2), (99.9, 100.0)), # impossible future timestamps
    ((99.9, 99.8), (99.9, 100.0)),   # invalid interval
    ((float("nan"), 99.9), (99.9, 100.0)),
    (None, (99.9, 100.0)),
    ((99.8, 99.9), None),
])
def test_invalid_or_delayed_samples(hub, inverter_window, meter_window):
    sample(hub, inverter_window=inverter_window, meter_window=meter_window)
    hub._apply_modbus_load_data()
    assert hub.data["load"] is None


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "123", True])
@pytest.mark.parametrize("field", ["ac", "meter"])
def test_invalid_power(hub, bad, field):
    sample(hub, **{field: bad})
    hub._apply_modbus_load_data()
    assert hub.data["load"] is None


@pytest.mark.parametrize("charge", [0, 12.8, 4000])
@pytest.mark.parametrize(("ac", "meter"), [(5580, -5751), (1, -1.01), (0, -5000)])
def test_negative_never_clamped_or_cached(hub, ac, meter, charge):
    sample(hub)
    hub._apply_modbus_load_data()
    assert hub.data["load"] == 1000
    sample(hub, ac=ac, meter=meter)
    hub._client.storage_configured = True
    hub.data["storage_charge_power"] = charge
    for _ in range(3):
        hub._apply_modbus_load_data()
        assert hub.data["load"] == pytest.approx(ac + meter, abs=0.005)
    sample(hub, ac=500, meter=100)
    hub._apply_modbus_load_data()
    assert hub.data["load"] == 600


@pytest.mark.parametrize("location", [1, 256, 511])
@pytest.mark.parametrize(("meter", "expected"), [(-750, 750), (100, -100), (0, 0)])
def test_load_and_subload_meters(hub, location, meter, expected):
    sample(hub, location=location, meter=meter, ac=None, inverter_window=None)
    hub._apply_modbus_load_data()
    assert hub.data["load"] == expected


@pytest.mark.parametrize("location", [None, 3, 2, 255, 512])
def test_other_meter_locations(hub, location):
    sample(hub, location=location)
    hub._apply_modbus_load_data()
    assert hub.data["load"] is None


def test_warning_rate_limit_and_recovery(hub, clock, caplog):
    caplog.set_level(logging.DEBUG)
    sample(hub, meter=None)
    for _ in range(20):
        hub._apply_modbus_load_data()
    warnings = lambda: [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings()) == 1
    assert "missing, invalid or stale meter sample" in warnings()[0].message
    clock.now += 301
    sample(hub, meter=None,
           inverter_window=(400.8, 400.9), meter_window=(400.9, 401.0))
    hub._apply_modbus_load_data()
    assert len(warnings()) == 2


@pytest.mark.asyncio
async def test_actual_coordinator_primary_first_and_optional_failures(hub, clock):
    client = hub._client
    client.meter_configured = True
    client._meter_unit_ids = [201, 200]
    events = []

    async def inverter():
        events.append("inverter")
        sample(hub)
        return True

    async def meter(unit_id, is_primary):
        events.append(f"meter {unit_id}")
        if not is_primary:
            assert hub.data["load"] == 1000
            clock.now += 5
            raise TimeoutError("secondary meter timeout")
        assert hub.data["load"] is None
        return True

    async def optional():
        events.append("optional")
        assert hub.data["load"] == 1000
        return True

    client.read_inverter_data = inverter
    client.read_meter_data = meter
    for name in ("read_inverter_status_data", "read_inverter_model_settings_data",
                 "read_inverter_controls_data", "read_ac_limit_data"):
        setattr(client, name, optional)
    coordinator = hub_module.FroniusCoordinator(None, hub)
    result = await coordinator._async_update_data()
    assert events[:3] == ["inverter", "meter 200", "meter 201"]
    assert result["load"] == 1000
    assert client.get_load_sample_windows(201) == ((99.8, 99.9), None)


@pytest.mark.asyncio
async def test_missing_primary_does_not_reuse_previous_load(hub):
    sample(hub)
    hub._apply_modbus_load_data()
    hub._client._meter_unit_ids = [201]
    hub._client.read_meter_data = AsyncMock(return_value=True)
    for name in ("read_inverter_status_data", "read_inverter_model_settings_data",
                 "read_inverter_controls_data", "read_ac_limit_data"):
        setattr(hub._client, name, AsyncMock(return_value=True))
    await hub._async_refresh_optional_data()
    assert hub.data["load"] is None
    assert hub.data["grid_status"] is None


@pytest.mark.asyncio
async def test_real_decoding_request_windows_and_recovery(hub, clock):
    client = hub._client
    client.meter_configured = True
    client.data["meter_200_location"] = 0
    inverter_regs = [0] * 50
    inverter_regs[12:16] = [5580, 0, 5000, 65534]
    inverter_regs[36:38] = [4, 4]
    meter_regs = [0] * 103
    meter_regs[14:17] = [5000, 65534, (-5751) & 65535]
    meter_regs[20] = 0
    client.get_registers = AsyncMock(side_effect=[inverter_regs, meter_regs])
    client.start_load_poll_cycle()
    assert await client.read_inverter_data()
    clock.now += .01
    assert await client.read_meter_data(200, is_primary=True)
    assert client.data["acpower"] == 5580
    assert client.data["meter_200_power"] == -5751
    assert client.get_load_sample_windows(200) == ((100, 100), (100.01, 100.01))
    hub._apply_modbus_load_data()
    assert hub.data["load"] == -171

    # Failed reads must invalidate success timestamps even within the same cycle.
    client.get_registers = AsyncMock(return_value=None)
    assert not await client.read_meter_data(200)
    assert client.get_load_sample_windows(200)[1] is None
    client.get_registers = AsyncMock(side_effect=TimeoutError("timeout"))
    assert not await client.read_inverter_data()
    assert client.get_load_sample_windows(200)[0] is None

    inverter_regs[12:14] = [5602, 0]
    meter_regs[16] = (-5572) & 65535
    client.get_registers = AsyncMock(side_effect=[inverter_regs, meter_regs])
    client.start_load_poll_cycle()
    assert client.get_load_sample_windows(200) == (None, None)
    assert await client.read_inverter_data()
    assert await client.read_meter_data(200, is_primary=True)
    hub._apply_modbus_load_data()
    assert hub.data["load"] == 30


@pytest.mark.asyncio
async def test_response_delay_includes_transport_wait(hub, clock):
    sample(hub)
    regs = [0] * 103
    regs[16] = 100

    async def delayed(**kwargs):
        clock.now += .8
        return regs

    hub._client.get_registers = delayed
    assert await hub._client.read_meter_data(200)
    assert hub._client.get_load_sample_windows(200)[1] == (100, 100.8)
    hub._apply_modbus_load_data()
    assert hub.data["load"] is None


@pytest.mark.parametrize(("raw", "sf", "expected"), [
    (29296, -1, 2929.6), (-27291, -1, -2729.1), (0, 0, 0),
    (-32768, 0, None), (-32768, -1, None), (100, -32768, None),
    (100, 32767, None), (100, -11, None), (100, 11, None),
])
def test_sunspec_signed_power_validation(hub, raw, sf, expected):
    assert hub._client._calculate_load_power(raw, sf) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["inverter", "meter"])
@pytest.mark.parametrize("sentinel", ["power", "scale_factor"])
async def test_sentinel_registers_remain_unknown(hub, kind, sentinel):
    sample(hub)
    regs = [0] * (50 if kind == "inverter" else 103)
    offset = 12 if kind == "inverter" else 16
    sf_offset = 13 if kind == "inverter" else 20
    regs[offset] = 100
    regs[offset if sentinel == "power" else sf_offset] = 0x8000
    hub._client.get_registers = AsyncMock(return_value=regs)
    if kind == "inverter":
        assert await hub._client.read_inverter_data()
        assert hub.data["acpower"] is None
    else:
        assert await hub._client.read_meter_data(200)
        assert hub.data["meter_200_power"] is None
    hub._apply_modbus_load_data()
    assert hub.data["load"] is None


@pytest.mark.asyncio
async def test_primary_timeout_does_not_poison_other_sensors(hub):
    sample(hub)
    hub._client.start_load_poll_cycle()
    hub._client.read_meter_data = AsyncMock(return_value=False)
    for name in ("read_inverter_status_data", "read_inverter_model_settings_data",
                 "read_inverter_controls_data", "read_ac_limit_data"):
        setattr(hub._client, name, AsyncMock(return_value=True))
    await hub._async_refresh_optional_data()
    assert hub.data["load"] is None
    assert hub.data["meter_200_power"] is None
    assert hub.data["acpower"] == 4000
    hub._client.read_ac_limit_data.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", [False, True])
async def test_core_failure_clears_load_and_preserves_transition_behavior(hub, transition):
    sample(hub)
    hub._apply_modbus_load_data()
    hub._client.read_inverter_data = AsyncMock(return_value=False)
    if transition:
        hub._battery_write_transition_until = 110
        result = await hub_module.FroniusCoordinator(None, hub)._async_update_data()
        assert result["load"] is None
    else:
        with pytest.raises(hub_module.UpdateFailed):
            await hub_module.FroniusCoordinator(None, hub)._async_update_data()
    assert hub.data["load"] is None
    assert hub._client.get_load_sample_windows(200) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("setter", "address"), [("set_charge_rate", 40356), ("set_discharge_rate", 40355)])
@pytest.mark.parametrize(("percent", "payload"), [(50, 5000), (-50, 60536), (0, 0)])
async def test_predbat_signed_rate_writes_unchanged(hub, setter, address, percent, payload):
    # Mock the transport: no physical write or battery mode change.
    hub._client.write_registers = AsyncMock()
    await getattr(hub._client, setter)(percent)
    hub._client.write_registers.assert_awaited_once_with(unit_id=1, address=address, payload=[payload])


@pytest.mark.parametrize(("inverter", "balcony", "household", "expected"), [
    (5000, 600, 200, -400),  # balcony output exceeds household consumption
    (5000, 600, 900, 300),   # positive residual still excludes balcony output
    (5000, 600, 600, 0),     # zero residual does not mean zero consumption
    (-3000, 600, 200, -400), # same result while charging the DC battery from AC
])
def test_additional_ac_generator_is_signed_net_demand(hub, caplog, inverter, balcony, household, expected):
    # Grid import is positive; generation by both inverters reduces that import.
    grid = household - inverter - balcony
    sample(hub, ac=inverter, meter=grid)
    hub._apply_modbus_load_data()
    assert hub.data["load"] == expected
    assert hub.data["load"] + balcony == household
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
