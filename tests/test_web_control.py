"""Web control: battery mode rules and the write side effects, with the HTTP client stubbed."""

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fronius_modbus.const import (
    API_USERNAME,
    DOMAIN,
    SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX,
)
from custom_components.fronius_modbus.froniuswebclient import FroniusWebAuthError
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
        return {"temperature": 41.5}

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
            "cell_temperature": 22.0,
        }

    def get_battery_config(self):
        return dict(self.battery)

    def get_export_limit_config(self):
        return {
            "exportLimits": {
                "activePower": {"softLimit": {"enabled": True, "powerLimit": 7000}}
            }
        }

    def set_battery_config(self, mode, power, soc_min=None):
        self.calls.append(("battery", mode, power, soc_min))
        self.battery.update(
            HYB_EM_MODE=mode, BAT_M0_SOC_MODE="manual" if mode else "auto"
        )
        return True

    def set_battery_soc_config(self, soc_min, soc_max, backup):
        self.calls.append(("soc", soc_min, soc_max, backup))
        return True

    def set_battery_charge_sources(self, grid, ac):
        self.calls.append(("sources", grid, ac))
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
        "technician_client": None,
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


async def test_refresh_fills_the_web_data(control):
    data = await control.async_refresh()
    assert data.inverter_temperature == 41.5
    assert (data.modbus_mode, data.modbus_control, data.modbus_restriction) == (
        "TCP",
        "enabled",
        "disabled",
    )
    assert data.battery_mode == "auto" and data.battery_mode_effective == 0
    assert data.export_soft_limit_w == 7000
    assert data.storage_temperature == 22.0


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


async def test_switching_to_manual_sends_power_and_soc_minimum(control):
    await control.async_refresh()
    await control.set_battery_mode(1)
    assert control._client.calls[-1] == ("battery", 1, 0, 5)
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


async def test_switching_back_to_manual_keeps_the_modbus_reserve(hass):
    """Manual mode must restore the Modbus reserve, not the 5 % Auto mode leaves behind.

    Leaving Manual resets the web API's own minimum to 5 %, so reading it back on
    the way in would silently discharge the battery below the user's reserve.
    """
    control = make_control(hass, modbus_soc_minimum=lambda: 7)
    try:
        await control.async_refresh()
        await control.set_battery_mode(1)
        await control.set_battery_mode(0)
        await control.set_battery_mode(1)
    finally:
        control.shutdown()

    assert control._client.calls[-1] == ("battery", 1, 0, 7)


async def test_the_export_soft_limit_is_shown_right_after_the_write(hass):
    """The web API is only re-read minutes later; until then the entity must not lie."""

    class FakeTechnicianClient(FakeWebClient):
        def set_export_soft_limit(self, watts):
            self.calls.append(("export", watts))
            return True

    pushed = []
    control = make_control(hass, technician_client=FakeTechnicianClient())
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

    control = make_control(hass, technician_client=FakeRejectingTechnicianClient())
    try:
        await control.async_refresh()
        with pytest.raises(RuntimeError, match="technician"):
            await control.set_export_soft_limit_w(4200)
    finally:
        control.shutdown()

    # The auth failure disables the technician client and clears the limit as
    # unknown; the point under test is that it never becomes the rejected 4200.
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
        assert soc_calls == [("soc", 10, 100, 5), ("soc", 10, 90, 5)]
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
