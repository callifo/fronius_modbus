"""A new entry's claim on its host, and which of a Datamanager's inverters it reads."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import datamanager
from .const import (
    CONF_API_USERNAME,
    CONF_DATAMANAGER,
    CONF_INVERTER_UNIT_ID,
    DEFAULT_INVERTER_UNIT_ID,
    WEB_API_DISABLED,
    entry_unique_id,
    inverter_number,
)
from .flow_forms import _set_form_error
from .flow_settings import (
    _entry_payload,
    async_check_role,
    async_look_at_device,
    check_login_on_owner,
)
from .flow_state import _CannotConnectModbus
from .host_sharing import host_holder, is_datamanager_host, taken_units, unique_id_for


async def async_free_units(hass: HomeAssistant, host: str) -> list[int] | None:
    """The units of the listed inverters no entry reads; None when none is listed."""
    listed = await hass.async_add_executor_job(datamanager.read_inverter_units, host)
    if not listed:
        return None
    taken = taken_units(hass, host)
    return [unit for unit in listed if unit not in taken]


def _lowest_free_unit(taken: set[int]) -> int:
    """The first number without an entry, for a Datamanager that lists none now."""
    unit_id = DEFAULT_INVERTER_UNIT_ID
    while unit_id in taken:
        unit_id += 1
    return unit_id


def _inverter_schema(offered: list[str], default: str) -> Any:
    """The inverters to choose from; a number can be typed, one may be asleep."""
    selector = SelectSelector(
        SelectSelectorConfig(
            options=offered, custom_value=True, mode=SelectSelectorMode.DROPDOWN
        )
    )
    field = vol.Required(CONF_INVERTER_UNIT_ID, default=default)
    return vol.Schema({field: selector if offered else str})


class NewInverterMixin(config_entries.ConfigFlow):
    """The part of the config flow only a new entry goes through."""

    # What the question was raised for, and the inverters it offers.
    _inverter_settings: dict[str, Any] | None = None
    _offered_units: list[int] = []

    def _flow_entry(self) -> config_entries.ConfigEntry | None:
        """The entry this flow changes; none while it creates one."""
        raise NotImplementedError

    async def _async_check_inverter(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Read the inverter the settings name; raises when it does not answer."""
        raise NotImplementedError

    async def _async_set_up_inverter(
        self, settings: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Go on with the chosen inverter: to its login, or to the end."""
        raise NotImplementedError

    async def _async_claim_new_host(self, settings: dict[str, Any]) -> None:
        unique_id = entry_unique_id({CONF_HOST: settings[CONF_HOST]})
        # A card for this host gives way to the owner adding it by hand; two
        # flows adding it by hand still stop each other.
        for card in self._async_in_progress(match_context={"unique_id": unique_id}):
            if card["context"]["source"] == config_entries.SOURCE_ZEROCONF:
                self.hass.config_entries.flow.async_abort(card["flow_id"])
        await self._async_claim_unique_id(settings)

    async def _async_claim_unique_id(self, settings: dict[str, Any]) -> None:
        """Abort for a host another entry keeps these settings off."""
        await self.async_set_unique_id(unique_id_for(self.hass, settings))
        datamanager_known = is_datamanager_host(self.hass, settings[CONF_HOST])
        if datamanager_known and CONF_DATAMANAGER not in settings:
            # Which of its inverters this one is has not been asked yet.
            return
        # Home Assistant lets a host whose card was ignored be set up by hand.
        self._abort_if_unique_id_configured()
        if host_holder(self.hass, settings) is not None:
            raise data_entry_flow.AbortFlow("already_configured")

    async def _async_finish_user(
        self, settings: dict[str, Any], info: dict[str, Any], previous_host: str | None
    ) -> config_entries.ConfigFlowResult:
        del previous_host
        await self._async_claim_unique_id(settings)
        return self.async_create_entry(
            title=info["title"],
            data=_entry_payload(settings, reconfigure_required=False)
            | ({CONF_DATAMANAGER: True} if settings.get(CONF_DATAMANAGER) else {}),
        )

    async def _async_look_at_device(
        self, settings: dict[str, Any]
    ) -> config_entries.ConfigFlowResult | None:
        """Ask a new entry's device what it is, and which of its inverters is meant."""
        if (entry := self._flow_entry()) is not None:
            await async_check_role(self.hass, settings, entry)
            return None
        found = await async_look_at_device(self.hass, settings)
        question = await self._async_inverter_question(settings) if found else None
        if question is None:
            await self._async_claim_new_host(settings)
        return question

    async def _async_inverter_question(
        self, settings: dict[str, Any]
    ) -> config_entries.ConfigFlowResult | None:
        """The question which inverter; none for the only one with the usual number."""
        host = settings[CONF_HOST]
        free = await async_free_units(self.hass, host)
        taken = taken_units(self.hass, host)
        if free == []:
            return self.async_abort(reason="already_configured")
        if not taken and free == [DEFAULT_INVERTER_UNIT_ID]:
            return None
        self._inverter_settings = settings
        self._offered_units = free or []
        default = free[0] if free else _lowest_free_unit(taken)
        return self._show_inverter_form(inverter_number(default), {})

    def _show_inverter_form(
        self, default: str, errors: dict[str, str]
    ) -> config_entries.ConfigFlowResult:
        assert self._inverter_settings is not None
        offered = [inverter_number(unit) for unit in self._offered_units]
        return self.async_show_form(
            step_id="inverter",
            data_schema=_inverter_schema(offered, default),
            errors=errors,
            description_placeholders={"host": self._inverter_settings[CONF_HOST]},
        )

    async def async_step_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Set up the inverter with the number the owner chose."""
        if user_input is None or self._inverter_settings is None:
            return await self.async_step_user()
        number = str(user_input.get(CONF_INVERTER_UNIT_ID, "")).strip()
        unit_id = datamanager.unit_of(number)
        errors: dict[str, str] = {}
        if unit_id is None:
            errors[CONF_INVERTER_UNIT_ID] = "invalid_inverter_number"
            return self._show_inverter_form(number, errors)
        settings = {**self._inverter_settings, CONF_INVERTER_UNIT_ID: unit_id}
        try:
            await self._async_claim_new_host(settings)
            # Over Modbus alone: the login, if any, comes after it.
            await self._async_check_inverter(
                {**settings, CONF_API_USERNAME: WEB_API_DISABLED}
            )
            check_login_on_owner(self.hass, settings, None)
            return await self._async_set_up_inverter(settings)
        except data_entry_flow.AbortFlow:
            raise
        except _CannotConnectModbus:
            errors[CONF_INVERTER_UNIT_ID] = "inverter_not_found"
        except Exception as err:  # pylint: disable=broad-except
            _set_form_error(errors, err)
        return self._show_inverter_form(number, errors)
