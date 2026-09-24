"""Web control: battery mode rules and the write side effects, with the HTTP client stubbed."""

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fronius_modbus.const import (
    API_USERNAME,
    DOMAIN,
    SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX,
)
from custom_components.fronius_modbus.froniuswebclient import (
    FroniusWebAuthError,
    FroniusWebResponseError,
)
from custom_components.fronius_modbus.fronius_modbus_api.exceptions import (
    ControlRefused,
)
from custom_components.fronius_modbus.token_store import async_get_token_store
from custom_components.fronius_modbus.web_control import FroniusWebControl
from homeassistant.helpers import issue_registry as ir


class FakeWebClient:
    def __init__(self):
        self.battery = {
            "HYB_EM_MODE": 0,
            "HYB_EM_POWER": 0,
            "BAT_M0_SOC_MODE": "auto",
            "BAT_M0_SOC_MIN": 5,
            "BAT_M0_SOC_MAX": 100,
            "HYB_BACKUP_RESERVED": 5,
            "HYB_BM_CHARGEFROMAC": False,
            "HYB_EVU_CHARGEFROMGRID": False,
        }
        self.calls = []

    def get_inverter_info(self):
        return {"readings": {"DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 41.5}}

    def get_modbus_config(self):
        return {
            "slave": {
                "mode": "tcp",
                "sunspecMode": "int+sf",
                "ctr": {"on": True, "restriction": {"on": False, "ip": ""}},
            }
        }

    def get_solar_api_config(self):
        return {"SolarAPIv1Enabled": False}

    def get_storage_info(self):
        return {
            "manufacturer": "BYD",
            "model": "HVS",
            "serial": "S",
            "readings": {"BAT_TEMPERATURE_CELL_F64": 22.0},
        }

    def get_battery_config(self):
        return dict(self.battery)

    def get_export_limit_config(self):
        return {
            "exportLimits": {
                "activePower": {"softLimit": {"enabled": True, "powerLimit": 7000}}
            }
        }

    def set_battery_config(self, mode, power=None):
        self.calls.append(("battery", mode, power))
        self.battery.update(HYB_EM_MODE=mode)
        return True

    def check_soc_window(self, soc_min=None, soc_max=None):
        lower = self.battery["BAT_M0_SOC_MIN"] if soc_min is None else soc_min
        upper = self.battery["BAT_M0_SOC_MAX"] if soc_max is None else soc_max
        if lower > upper:
            raise ControlRefused("soc_minimum_above_maximum", "inverted window")
        return dict(self.battery)

    def set_soc_limits(self, soc_min=None, soc_max=None):
        self.check_soc_window(soc_min, soc_max)
        self.calls.append(("soc", soc_min, soc_max))
        limits = {"BAT_M0_SOC_MIN": soc_min, "BAT_M0_SOC_MAX": soc_max}
        self.battery.update({k: v for k, v in limits.items() if v is not None})
        return True

    def set_battery_power(self, power):
        self.calls.append(("power", power))
        return True

    def set_battery_charge_sources(self, charge_from_grid=None, charge_from_ac=None):
        self.calls.append(("sources", charge_from_grid, charge_from_ac))
        return True

    def set_soc_mode(self, mode):
        self.calls.append(("soc_mode", mode))
        self.battery["BAT_M0_SOC_MODE"] = mode
        return True

    def set_backup_reserve(self, percent):
        self.calls.append(("reserve", percent))
        self.battery["HYB_BACKUP_RESERVED"] = percent
        return True

    def set_solar_api_enabled(self, enabled):
        self.calls.append(("solar", enabled))
        return True

    def reset_modbus_control(self):
        self.calls.append(("reset",))
        return True


def make_control(hass, **kwargs) -> FroniusWebControl:
    """A web control on a fake client, with the write events collected on `events`."""
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.0.2.1"}, title="Fronius")
    entry.add_to_hass(hass)
    events: list[str] = []
    defaults = {
        "client": FakeWebClient(),
        "api_username": "customer",
        "storage_present": True,
        "inverter_firmware": lambda: "1.38.6-1",
    }
    control = FroniusWebControl(
        hass,
        entry,
        host="192.0.2.1",
        on_battery_write=lambda: events.append("write"),
        **(defaults | kwargs),
    )
    control.events = events
    return control


@pytest.fixture
def control(hass):
    control = make_control(hass)
    yield control
    control.shutdown()


class FakeClientAlreadyThere(FakeWebClient):
    """Every battery setting the control asks for is already on the inverter."""

    def set_battery_config(self, mode, power=None):
        super().set_battery_config(mode, power)
        return False

    def set_soc_mode(self, mode):
        super().set_soc_mode(mode)
        return False

    def set_backup_reserve(self, percent):
        super().set_backup_reserve(percent)
        return False

    def set_battery_charge_sources(self, grid, ac):
        super().set_battery_charge_sources(grid, ac)
        return False


@pytest.mark.parametrize(
    ("setter", "arguments"),
    [
        ("set_battery_mode", {"mode": 0}),
        ("set_soc_mode", {"manual": False}),
        ("set_backup_reserve", {"percent": 5}),
        ("set_charge_sources", {"charge_from_grid": False}),
    ],
)
async def test_a_battery_write_that_changed_nothing_opens_no_recovery_window(
    hass, setter, arguments
):
    """Tolerating Modbus failures and a delayed refresh follow a real write only."""
    control = make_control(hass, client=FakeClientAlreadyThere())
    try:
        await control.async_refresh()
        await getattr(control, setter)(**arguments)
    finally:
        control.shutdown()

    assert control.events == []


class FakeClientWithReadings(FakeWebClient):
    def get_inverter_info(self):
        return {"readings": {"FANCONTROL_PERCENT_01_F32": 12.0}}

    def get_storage_info(self):
        return super().get_storage_info() | {"readings": {"sw_version": "3.26"}}


async def test_the_refresh_hands_on_the_component_readings(hass):
    control = make_control(hass, client=FakeClientWithReadings())
    try:
        data = await control.async_refresh()
    finally:
        control.shutdown()

    assert data.inverter_readings == {"FANCONTROL_PERCENT_01_F32": 12.0}
    assert data.storage_readings == {"sw_version": "3.26"}


class FakeClientWithoutComponents(FakeWebClient):
    def get_inverter_info(self):
        return {"readings": None, "missing": True}

    def get_storage_info(self):
        return super().get_storage_info() | {"readings": None, "missing": True}


async def test_the_refresh_hands_on_missing_component_endpoints(hass):
    control = make_control(hass, client=FakeClientWithoutComponents())
    try:
        data = await control.async_refresh()
    finally:
        control.shutdown()

    assert (data.inverter_endpoint_missing, data.storage_endpoint_missing) == (
        True,
        True,
    )


async def test_refresh_fills_the_web_data(control):
    data = await control.async_refresh()
    assert data.inverter_readings == {"DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 41.5}
    assert (data.modbus_mode, data.modbus_control, data.modbus_restriction) == (
        "TCP",
        "enabled",
        "disabled",
    )
    assert data.battery_mode == "auto" and data.battery_mode_effective == 0
    assert data.export_soft_limit_w == 7000
    assert data.storage_readings == {"BAT_TEMPERATURE_CELL_F64": 22.0}


async def test_auto_mode_with_a_manual_soc_mode_still_reads_as_auto(hass):
    """The inverter leaves BAT_M0_SOC_MODE at "manual" after any SoC write."""
    client = FakeWebClient()
    client.battery.update(HYB_EM_MODE=0, BAT_M0_SOC_MODE="manual")
    control = make_control(hass, client=client)
    data = await control.async_refresh()
    assert data.battery_mode_effective == 0
    assert data.battery_mode == "auto"
    assert control.battery_mode_is_manual is False
    control.shutdown()


async def test_switching_to_manual_keeps_the_target_on_the_inverter(control):
    """Audit F24-01: the cached target went along and undid one set since the poll."""
    await control.async_refresh()
    await control.set_battery_mode(1)
    assert control._client.calls[-1] == ("battery", 1, None)
    assert control.battery_mode_is_manual
    assert control.events == ["write"]


async def test_battery_power_needs_manual_mode(control):
    await control.async_refresh()
    with pytest.raises(ValueError, match="Manual"):
        await control.set_battery_power_w(500)


async def test_charge_from_grid_implies_charge_from_ac(control):
    await control.async_refresh()
    await control.set_charge_sources(charge_from_grid=True)
    assert control._client.calls[-1] == ("sources", True, True)
    assert (control.data.charge_from_grid, control.data.charge_from_ac) == (True, True)


@pytest.mark.parametrize(
    ("request_", "sent"),
    [
        ({"charge_from_ac": True}, ("sources", None, True)),
        ({"charge_from_ac": False}, ("sources", False, False)),
        ({"charge_from_grid": False}, ("sources", False, None)),
    ],
)
async def test_a_charge_source_sends_only_itself_and_what_it_implies(
    control, request_, sent
):
    """Audit F24-01: the other flag came from the cache and could undo a fresh change."""
    await control.async_refresh()
    await control.set_charge_sources(**request_)
    assert control._client.calls[-1] == sent


async def test_the_target_feed_in_sends_only_the_power(hass):
    control = make_control(hass)
    try:
        await control.async_refresh()
        await control.set_battery_mode(1)
        await control.set_battery_power_w(500)
    finally:
        control.shutdown()
    assert control._client.calls[-1] == ("power", -500)


async def test_the_export_soft_limit_is_shown_right_after_the_write(hass):
    """The web API is only re-read minutes later; until then the entity must not lie."""

    class FakeTechnicianClient(FakeWebClient):
        def set_export_soft_limit(self, watts):
            self.calls.append(("export", watts))
            return True

    pushed = []
    control = make_control(
        hass, client=FakeTechnicianClient(), api_username="technician"
    )
    control.attach_coordinator(
        type("Coordinator", (), {"async_set_updated_data": pushed.append})()
    )
    try:
        await control.set_export_soft_limit_w(4200.4)
    finally:
        control.shutdown()

    assert control.data.export_soft_limit_w == 4200
    assert pushed[-1].export_soft_limit_w == 4200


async def test_a_rejected_technician_write_does_not_fake_the_limit(hass):
    """An auth failure on the write must not leave the entity showing the unapplied value."""

    class FakeRejectingTechnicianClient(FakeWebClient):
        def set_export_soft_limit(self, watts):
            raise FroniusWebAuthError("token rejected")

    control = make_control(
        hass, client=FakeRejectingTechnicianClient(), api_username="technician"
    )
    try:
        await control.async_refresh()
        with pytest.raises(RuntimeError, match="authentication failed"):
            await control.set_export_soft_limit_w(4200)
    finally:
        control.shutdown()

    # The auth failure disables the web client and clears the limit as unknown;
    # the point under test is that it never becomes the rejected 4200.
    assert control.data.export_soft_limit_w != 4200


async def test_the_export_limit_needs_the_technician_client(control):
    with pytest.raises(RuntimeError, match="Technician"):
        await control.set_export_soft_limit_w(4200)


async def test_an_auth_failure_disables_the_web_api_and_deletes_the_token(hass):
    """A rejected token must not be retried forever; the user has to reconfigure."""

    class FakeAuthFailingClient(FakeWebClient):
        def get_inverter_info(self):
            raise FroniusWebAuthError("token rejected")

    await async_get_token_store(hass).async_save_token(
        "192.0.2.1", API_USERNAME, "stale-token"
    )
    control = make_control(hass, client=FakeAuthFailingClient())
    try:
        await control.async_refresh()
    finally:
        control.shutdown()

    assert control.configured is False
    assert (
        await async_get_token_store(hass).async_load_token("192.0.2.1", API_USERNAME)
        is None
    )
    assert any(
        issue.domain == DOMAIN and issue.issue_id.endswith(control._entry.entry_id)
        for issue in ir.async_get(hass).issues.values()
    )


async def test_a_firmware_update_clears_the_solar_api_warning_on_the_next_refresh(hass):
    """The firmware is read per sync, so an update must not need a restart to be noticed."""

    class SolarApiOnClient(FakeWebClient):
        def get_solar_api_config(self):
            return {"SolarAPIv1Enabled": True}

    firmware = ["1.38.6-1"]
    control = make_control(
        hass,
        client=SolarApiOnClient(),
        inverter_firmware=lambda: firmware[0],
    )
    issue_id = f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}{control._entry.entry_id}"
    try:
        await control.async_refresh()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

        firmware[0] = "1.40.7-1"
        await control.async_refresh()
    finally:
        control.shutdown()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_concurrent_soc_writes_are_applied_one_after_the_other(hass):
    """Audit F10: two read-modify-write SoC updates composed from the same old tuple lost one change."""
    control = make_control(hass)
    try:
        await control.async_refresh()
        control._set_effective_battery_mode(1, "manual")
        await asyncio.gather(
            control.set_soc_minimum_manual(10), control.set_soc_maximum(90)
        )
        soc_calls = [call for call in control._client.calls if call[0] == "soc"]
        assert soc_calls == [("soc", 10, None), ("soc", None, 90)]
        assert (control.data.soc_min, control.data.soc_max) == (10, 90)
    finally:
        control.shutdown()


async def test_a_write_without_a_customer_login_is_an_error_not_a_silent_no_op(hass):
    """Audit F16: setters returned quietly after the login had been rejected."""
    control = make_control(hass, client=None)
    try:
        with pytest.raises(RuntimeError, match="not configured"):
            await control.set_solar_api_enabled(False)
    finally:
        control.shutdown()


async def test_the_technician_role_is_the_single_client_with_that_username(hass):
    """Upstream #130: no second client; the role of the one login decides what is exposed."""

    class FakeTechnicianClient(FakeWebClient):
        def set_export_soft_limit(self, watts):
            self.calls.append(("export", watts))
            return True

    control = make_control(
        hass, client=FakeTechnicianClient(), api_username="technician"
    )
    try:
        assert control.technician_configured
        await control.set_export_soft_limit_w(4200)
    finally:
        control.shutdown()
    assert control._client.calls[-1] == ("export", 4200)


async def test_a_customer_client_is_not_technician_configured(control):
    assert control.configured
    assert not control.technician_configured


async def test_the_backup_reserve_is_written_in_auto_mode_too(control):
    """The inverter's own UI offers the reserve in either battery mode."""
    await control.async_refresh()
    await control.set_backup_reserve(30)
    assert control._client.calls[-1] == ("reserve", 30)
    assert control.data.backup_reserved == 30
    assert control.events == ["write"]


async def test_a_backup_reserve_outside_the_range_is_refused(control):
    await control.async_refresh()
    with pytest.raises(ValueError):
        await control.set_backup_reserve(4)
    assert control._client.calls == []
    assert control.data.backup_reserved == 5


async def test_the_soc_window_follows_the_soc_mode_not_the_energy_management(hass):
    """The inverter's own UI: SoC limits have their own automatic/manual switch.

    A user who sets the limits to manual and leaves self-consumption optimisation
    on automatic must still be able to write the minimum and the maximum.
    """
    client = FakeWebClient()
    client.battery.update(HYB_EM_MODE=0, BAT_M0_SOC_MODE="manual")
    control = make_control(hass, client=client)
    try:
        await control.async_refresh()
        assert control.soc_mode_is_manual
        await control.set_soc_maximum(90)
        await control.set_soc_minimum_manual(12)
    finally:
        control.shutdown()
    assert [c for c in client.calls if c[0] == "soc"] == [
        ("soc", None, 90),
        ("soc", 12, None),
    ]


async def test_switching_the_energy_management_leaves_the_soc_window_alone(control):
    """Auto/Manual self-consumption optimisation is not the SoC limits switch."""
    control._client.battery.update(BAT_M0_SOC_MODE="manual", BAT_M0_SOC_MIN=20)
    await control.async_refresh()
    await control.set_battery_mode(1)
    await control.set_battery_mode(0)
    assert (control.data.soc_min, control.data.soc_mode_raw) == (20, "manual")
    assert [c for c in control._client.calls if c[0] == "battery"] == [
        ("battery", 1, None),
        ("battery", 0, None),
    ]


async def test_the_modbus_reserve_mirrors_to_the_web_api_in_manual_soc_mode(hass):
    client = FakeWebClient()
    client.battery.update(HYB_EM_MODE=0, BAT_M0_SOC_MODE="manual")
    control = make_control(hass, client=client)
    written = []
    try:
        await control.async_refresh()

        async def write_modbus():
            written.append(9)

        await control.apply_soc_minimum(9, write_modbus)
    finally:
        control.shutdown()
    assert written == [9]
    assert client.calls[-1] == ("soc", 9, None)


async def test_the_soc_mode_select_opens_the_window_for_writing(control):
    await control.async_refresh()
    assert not control.soc_mode_is_manual
    await control.set_soc_mode(manual=True)
    assert control._client.calls[-1] == ("soc_mode", "manual")
    assert control.soc_mode_is_manual
    assert control.data.soc_mode == "manual"
    assert control.events == ["write"]
    await control.set_soc_mode(manual=False)
    assert control._client.calls[-1] == ("soc_mode", "auto")
    assert not control.soc_mode_is_manual


def _hold_the_first_write(control) -> asyncio.Event:
    """Park the first web write until released, so a burst can pile up behind it."""
    release = asyncio.Event()
    job = control._async_web_job
    held = []

    async def holding_job(func, *args, **kwargs):
        if not held:
            held.append(True)
            await release.wait()
        return await job(func, *args, **kwargs)

    control._async_web_job = holding_job
    return release


async def test_a_burst_of_values_reaches_the_device_as_first_and_last(control):
    """Ten input-box steps were ten web writes; the inverter stalls Modbus after each."""
    await control.async_refresh()
    release = _hold_the_first_write(control)
    burst = [
        asyncio.ensure_future(control.set_backup_reserve(p))
        for p in (10, 20, 30, 40, 50)
    ]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*burst)
    assert [c for c in control._client.calls if c[0] == "reserve"] == [
        ("reserve", 10),
        ("reserve", 50),
    ]
    assert control.data.backup_reserved == 50


async def test_the_last_writer_of_a_burst_gets_the_error(control):
    await control.async_refresh()
    written = control._client.set_backup_reserve

    def refuse_fifty(percent):
        if percent == 50:
            raise FroniusWebResponseError("HTTP 500", 500)
        return written(percent)

    control._client.set_backup_reserve = refuse_fifty
    release = _hold_the_first_write(control)
    burst = [
        asyncio.ensure_future(control.set_backup_reserve(p))
        for p in (10, 20, 30, 40, 50)
    ]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*burst, return_exceptions=True)
    assert results[:4] == [None] * 4
    assert isinstance(results[4], FroniusWebResponseError)
    assert control.data.backup_reserved == 10


async def test_different_controls_do_not_supersede_each_other(control):
    control._client.battery.update(HYB_EM_MODE=0, BAT_M0_SOC_MODE="manual")
    await control.async_refresh()
    release = _hold_the_first_write(control)
    both = [
        asyncio.ensure_future(control.set_backup_reserve(30)),
        asyncio.ensure_future(control.set_soc_maximum(90)),
    ]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*both)
    assert [c for c in control._client.calls if c[0] in ("reserve", "soc")] == [
        ("reserve", 30),
        ("soc", None, 90),
    ]


async def test_every_battery_write_shows_its_value_right_away(hass):
    """Discussion #6: a select snapped back to the old value until the delayed refresh.

    The control's own data was updated at once, the coordinator's copy only ten
    seconds later, so the entity showed the write undone for that long.
    """
    pushed = []
    control = make_control(hass)
    control.attach_coordinator(
        type("Coordinator", (), {"async_set_updated_data": pushed.append})()
    )
    try:
        await control.async_refresh()
        await control.set_battery_mode(1)
        assert pushed[-1].battery_mode == "manual"
        await control.set_soc_mode(manual=True)
        assert pushed[-1].soc_mode == "manual"
        await control.set_backup_reserve(30)
        assert pushed[-1].backup_reserved == 30
        await control.set_charge_sources(charge_from_grid=True)
        assert pushed[-1].charge_from_grid is True
        await control.set_solar_api_enabled(True)
        assert pushed[-1].solar_api_enabled is True
    finally:
        control.shutdown()


async def test_a_soc_minimum_the_inverter_refuses_is_not_written_to_modbus_first(hass):
    """Reaudit RE26-02: a stale poll passed, Modbus took the minimum, the web API refused it."""
    client = FakeWebClient()
    client.battery.update(
        BAT_M0_SOC_MODE="manual", BAT_M0_SOC_MIN=20, BAT_M0_SOC_MAX=90
    )
    control = make_control(hass, client=client)
    written = []
    try:
        await control.async_refresh()
        client.battery.update(BAT_M0_SOC_MAX=30)

        async def write_modbus():
            written.append(50)

        with pytest.raises(ControlRefused) as refused:
            await control.apply_soc_minimum(50, write_modbus)
    finally:
        control.shutdown()
    assert refused.value.key == "soc_minimum_above_maximum"
    assert written == []


async def test_a_soc_maximum_a_stale_poll_would_refuse_reaches_the_inverter(hass):
    """Reaudit RE26-02: the polled minimum 50 refused a maximum the inverter would take."""
    client = FakeWebClient()
    client.battery.update(
        BAT_M0_SOC_MODE="manual", BAT_M0_SOC_MIN=50, BAT_M0_SOC_MAX=90
    )
    control = make_control(hass, client=client)
    try:
        await control.async_refresh()
        client.battery.update(BAT_M0_SOC_MIN=20)
        await control.set_soc_maximum(30)
    finally:
        control.shutdown()
    assert client.calls[-1] == ("soc", None, 30)
