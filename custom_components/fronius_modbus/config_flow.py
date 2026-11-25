from __future__ import annotations

import asyncio
from functools import partial
import logging
from typing import Any, Awaitable, Callable

from modbus_connection import ModbusError, ModbusTcpParams
import voluptuous as vol

from homeassistant import config_entries, data_entry_flow
from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_AUTO_ENABLE_MODBUS,
    CONF_DATAMANAGER,
    CONF_INVERTER_UNIT_ID,
    CONF_METER_POWER_INTERVAL,
    CONF_MODBUS_RESTRICTION,
    DEFAULT_AUTO_ENABLE_MODBUS,
    DEFAULT_METER_UNIT_ID,
    DOMAIN,
    MINIMUM_SCAN_INTERVAL,
    API_USERNAME,
    SUPPORTED_MANUFACTURERS,
    SUPPORTED_MODELS,
    WEB_API_DISABLED,
    ModbusRestriction,
    entry_title,
    entry_title_after,
)
from .datamanager_client import DatamanagerClient
from .discovery import (
    async_follow_host,
    discovered_model,
    discovered_serial,
    entry_for_serial,
)
from .flow_entry import (
    _async_reload_unless_listened,
    _claim_host,
    async_update_entry_from_input,
)
from .flow_forms import (
    CONF_ALLOWED_HOST,
    _set_form_error,
    allowed_host_schema,
    asks_for_the_address,
    checked_allowed_host,
    password_schema,
    role_schema,
    settings_schema,
)
from .flow_settings import (
    _default_payload,
    _entry_payload,
    _expand_settings_input,
    _should_apply_modbus_config,
    async_check_role,
    defaults_after,
    device_answer,
    entry_defaults,
)
from .flow_state import (
    _AddressesNotUnique,
    _AllowedHostNeeded,
    _AlreadyConfigured,
    _CannotConnect,
    _CannotConnectModbus,
    _InvalidApiCredentials,
    _InvalidHost,
    _InvalidPort,
    _MeterPowerIntervalTooLong,
    _MissingApiPassword,
    _PendingFlowState,
    _ScanIntervalTooShort,
    _SunSpecSwitchNeeded,
    _UnsupportedHardware,
)
from .fronius_modbus_api.device import FroniusInverter
from .fronius_modbus_api.exceptions import error_text
from .froniuswebclient import (
    ClientIpResolutionError,
    FroniusWebClient,
    FroniusWebResponseError,
    SunSpecModeChangeNeeded,
    mint_token,
)
from .inverter_choice import NewInverterMixin
from .token_store import (
    async_forget_unused_tokens,
    async_get_token_store,
    canonical_host,
)

_LOGGER = logging.getLogger(__name__)

type _FlowFinishCallback = Callable[
    [dict[str, Any], dict[str, Any], str | None],
    Awaitable[Any],
]


type _FlowRestartCallback = Callable[[], Awaitable[Any]]


type _HostClaim = Callable[[dict[str, Any]], Awaitable[None]]


def _validate_static_input(data: dict[str, Any]) -> None:
    if len(data[CONF_HOST]) < 3:
        raise _InvalidHost
    if data[CONF_PORT] > 65535:
        raise _InvalidPort
    if data[CONF_SCAN_INTERVAL] < MINIMUM_SCAN_INTERVAL:
        raise _ScanIntervalTooShort
    meter_power_interval = data[CONF_METER_POWER_INTERVAL]
    # The sensor shows the fast poll's reading while it succeeds; a slower one
    # would hide the full poll's newer reading.
    if meter_power_interval and meter_power_interval >= data[CONF_SCAN_INTERVAL]:
        raise _MeterPowerIntervalTooLong

    all_addresses = [DEFAULT_METER_UNIT_ID, data[CONF_INVERTER_UNIT_ID]]
    if len(all_addresses) > len(set(all_addresses)):
        _LOGGER.error("Modbus addresses are not unique %s", all_addresses)
        raise _AddressesNotUnique


async def _async_mint_token(
    hass: HomeAssistant,
    host: str,
    password: str,
    username: str = API_USERNAME,
    *,
    on_datamanager: bool = False,
) -> dict[str, str]:
    password = str(password).strip()
    if password == "":
        raise _MissingApiPassword

    job: Callable[[], dict[str, str] | None] = partial(
        mint_token, host, username, password
    )
    if on_datamanager:
        job = DatamanagerClient(host, password=password).login
    try:
        token = await hass.async_add_executor_job(job)
    except Exception as err:
        raise _CannotConnect from err

    if not token:
        raise _InvalidApiCredentials
    return token


async def _async_address_to_allow(
    hass: HomeAssistant, client: FroniusWebClient, *, ask: bool
) -> str:
    """Home Assistant's own address, when the list allows it and the owner did not ask.

    Raises ``_AllowedHostNeeded`` otherwise: behind NAT the inverter sees
    another address than this one, and allowing the wrong one locks every
    Modbus control out.
    """
    try:
        resolved_ip = await hass.async_add_executor_job(client.resolve_client_ip)
    except ClientIpResolutionError:
        # The owner can still type the address the inverter sees.
        resolved_ip = ""
    if ask or not resolved_ip:
        raise _AllowedHostNeeded(resolved_ip)
    # Asked only when the write would change the list: Reconfigure and
    # reauthentication apply the settings every time.
    if not await hass.async_add_executor_job(client.restriction_allows, resolved_ip):
        raise _AllowedHostNeeded(resolved_ip)
    return resolved_ip


async def _async_prepare_web_api(
    hass: HomeAssistant,
    data: dict[str, Any],
    api_token: dict[str, str] | None,
    apply_modbus_config: bool,
    claim_host: Callable[[], Awaitable[None]] | None,
    switch_sunspec_mode: bool,
    allowed_host: str | None,
    *,
    ask_allowed_host: bool,
) -> None:
    """Log in, and switch Modbus on where the flow asks for it.

    ``claim_host`` runs right before the Modbus settings are written: another
    entry can take the host while the token is minted or the login runs, and
    the settings would then change on an inverter this flow does not own.
    """
    if data.get(CONF_DATAMANAGER):
        datamanager_client = DatamanagerClient(data[CONF_HOST], token=api_token)
        if not await hass.async_add_executor_job(datamanager_client.login):
            raise _InvalidApiCredentials
        return
    client = FroniusWebClient(
        host=data[CONF_HOST],
        username=data[CONF_API_USERNAME],
        password="",
        token=api_token,
    )
    if not await hass.async_add_executor_job(client.login):
        raise _InvalidApiCredentials
    enable_modbus = data.get(CONF_AUTO_ENABLE_MODBUS, DEFAULT_AUTO_ENABLE_MODBUS)
    if not (apply_modbus_config and enable_modbus):
        return
    restricted = data[CONF_MODBUS_RESTRICTION] == ModbusRestriction.HOME_ASSISTANT
    if restricted and allowed_host is None:
        allowed_host = await _async_address_to_allow(hass, client, ask=ask_allowed_host)
    if claim_host is not None:
        await claim_host()
    try:
        await hass.async_add_executor_job(
            client.ensure_modbus_enabled,
            data[CONF_PORT],
            DEFAULT_METER_UNIT_ID,
            data[CONF_INVERTER_UNIT_ID],
            data[CONF_MODBUS_RESTRICTION],
            switch_sunspec_mode,
            allowed_host,
        )
    except SunSpecModeChangeNeeded as err:
        raise _SunSpecSwitchNeeded(err.current_mode, allowed_host) from err
    # The inverter restarts its Modbus server after the settings write.
    await asyncio.sleep(1.0)


async def _validate_input(
    hass: HomeAssistant,
    data: dict[str, Any],
    *,
    api_token: dict[str, str] | None = None,
    apply_modbus_config: bool = False,
    claim_host: Callable[[], Awaitable[None]] | None = None,
    switch_sunspec_mode: bool = False,
    allowed_host: str | None = None,
    ask_allowed_host: bool = False,
) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    _validate_static_input(data)

    web_api = data[CONF_API_USERNAME] != WEB_API_DISABLED

    try:
        if web_api:
            await _async_prepare_web_api(
                hass,
                data,
                api_token,
                apply_modbus_config,
                claim_host,
                switch_sunspec_mode,
                allowed_host,
                ask_allowed_host=ask_allowed_host,
            )
        async with async_get_temporary_unit(
            hass,
            ModbusTcpParams(host=data[CONF_HOST], port=data[CONF_PORT]),
            data[CONF_INVERTER_UNIT_ID],
        ) as unit:
            identity = await FroniusInverter.async_probe(unit)
    except (
        _InvalidApiCredentials,
        _AlreadyConfigured,
        _SunSpecSwitchNeeded,
        _AllowedHostNeeded,
        data_entry_flow.AbortFlow,
    ):
        raise
    except (
        ModbusError,
        HomeAssistantError,
        FroniusWebResponseError,
        OSError,
        TimeoutError,
    ) as err:
        _LOGGER.error("Cannot reach inverter: %s", error_text(err))
        if not web_api:
            raise _CannotConnectModbus from err
        raise _CannotConnect from err

    if identity.manufacturer not in SUPPORTED_MANUFACTURERS:
        _LOGGER.error("Unsupported manufacturer: %r", identity.manufacturer)
        raise _UnsupportedHardware
    if not any(identity.model.startswith(model) for model in SUPPORTED_MODELS):
        _LOGGER.warning("Untested model %s", identity.model)

    return {"title": entry_title(data)}


class TokenFlowMixin:
    _pending_flow_state: _PendingFlowState | None = None
    hass: HomeAssistant

    async def _async_claim_entry_host(
        self, entry: config_entries.ConfigEntry, settings: dict[str, Any]
    ) -> None:
        _claim_host(self.hass, entry, settings)

    async def _async_look_at_device(self, settings: dict[str, Any]):
        """Check the role against the device; a question it raises is shown first."""
        await async_check_role(self.hass, settings)

    async def _async_show_password_step(
        self,
        *,
        step_id: str,
        errors: dict[str, str] | None = None,
    ):
        state = self._pending_flow_state
        placeholders = None
        # A step of its own: the role is called technician, its login service.
        if state is not None and state.settings.get(CONF_DATAMANAGER):
            step_id = f"{step_id}{SERVICE_LOGIN_STEP}"
        if state is not None:
            placeholders = {
                "entry_title": entry_title(state.settings),
                "host": str(state.settings.get(CONF_HOST, "")),
            }
        return self.async_show_form(
            step_id=step_id,
            data_schema=password_schema(
                keep_stored=state is not None and state.existing_token is not None
            ),
            errors=errors or {},
            description_placeholders=placeholders,
        )

    async def _async_show_sunspec_step(
        self,
        *,
        step_id: str,
        errors: dict[str, str] | None = None,
    ):
        state = self._pending_flow_state
        assert state is not None
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({}),
            errors=errors or {},
            description_placeholders={
                "entry_title": entry_title(state.settings),
                "host": str(state.settings.get(CONF_HOST, "")),
                "mode": str(state.sunspec_mode),
            },
        )

    async def _async_ask_sunspec_switch(
        self,
        needed: _SunSpecSwitchNeeded,
        state: _PendingFlowState,
        token: dict[str, str] | None,
        *,
        minted: bool,
        step_id: str,
    ):
        """Hold the flow where it stopped and ask before switching the model type."""
        state.login_token = token
        state.token_minted = minted
        state.sunspec_mode = needed.mode
        state.allowed_host = needed.allowed_host
        self._pending_flow_state = state
        return await self._async_show_sunspec_step(step_id=step_id)

    async def _async_handle_sunspec_step(
        self,
        *,
        user_input: dict[str, Any] | None,
        step_id: str,
        restart_step: _FlowRestartCallback,
        claim_host: _HostClaim,
        on_success: _FlowFinishCallback,
    ):
        """Switch the model type once the owner agreed, then finish as asked.

        Nothing was written before the question, and a flow left here writes
        nothing: a minted token is kept only when the flow finishes.
        """
        errors: dict[str, str] = {}
        state = self._pending_flow_state
        if state is None or state.sunspec_mode is None:
            return await restart_step()

        if user_input is not None:
            try:
                await claim_host(state.settings)
                info = await _validate_input(
                    self.hass,
                    state.settings,
                    api_token=state.login_token,
                    apply_modbus_config=state.apply_modbus_config,
                    claim_host=lambda: claim_host(state.settings),
                    switch_sunspec_mode=True,
                    allowed_host=state.allowed_host,
                )
                minted = state.login_token if state.token_minted else None
                # Cleared once the finish went through: a late error shows on
                # this form again, which needs the state.
                result = await self._async_finish_with_token(
                    on_success, state, info, minted
                )
                self._pending_flow_state = None
                return result
            except data_entry_flow.AbortFlow:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _set_form_error(errors, err)

        return await self._async_show_sunspec_step(step_id=step_id, errors=errors)

    async def _async_ask_before_writing(
        self,
        needed: _SunSpecSwitchNeeded | _AllowedHostNeeded,
        state: _PendingFlowState,
        token: dict[str, str] | None,
        *,
        minted: bool,
        step_ids: tuple[str, str],
    ):
        """Hold the flow for the question the Modbus settings raised before a write."""
        sunspec_step_id, allowed_host_step_id = step_ids
        if isinstance(needed, _AllowedHostNeeded):
            return await self._async_ask_allowed_host(
                needed, state, token, minted=minted, step_id=allowed_host_step_id
            )
        return await self._async_ask_sunspec_switch(
            needed, state, token, minted=minted, step_id=sunspec_step_id
        )

    async def _async_ask_allowed_host(
        self,
        needed: _AllowedHostNeeded,
        state: _PendingFlowState,
        token: dict[str, str] | None,
        *,
        minted: bool,
        step_id: str,
    ):
        """Hold the flow where it stopped and show the address before it is allowed."""
        state.login_token = token
        state.token_minted = minted
        state.resolved_host = needed.resolved_ip
        self._pending_flow_state = state
        return await self._async_show_allowed_host_step(step_id=step_id)

    async def _async_show_allowed_host_step(
        self,
        *,
        step_id: str,
        errors: dict[str, str] | None = None,
    ):
        state = self._pending_flow_state
        assert state is not None and state.resolved_host is not None
        return self.async_show_form(
            step_id=step_id,
            # A corrected address stays when an error shows the step again.
            data_schema=allowed_host_schema(state.allowed_host or state.resolved_host),
            errors=errors or {},
            description_placeholders={
                "entry_title": entry_title(state.settings),
                "host": str(state.settings.get(CONF_HOST, "")),
            },
        )

    async def _async_handle_allowed_host_step(
        self,
        *,
        user_input: dict[str, Any] | None,
        step_id: str,
        sunspec_step_id: str,
        restart_step: _FlowRestartCallback,
        claim_host: _HostClaim,
        on_success: _FlowFinishCallback,
    ):
        """Write the restriction with the confirmed address, then finish as asked."""
        errors: dict[str, str] = {}
        state = self._pending_flow_state
        if state is None or state.resolved_host is None:
            return await restart_step()

        if user_input is not None:
            try:
                state.allowed_host = checked_allowed_host(
                    user_input.get(CONF_ALLOWED_HOST, "")
                )
                await claim_host(state.settings)
                try:
                    info = await _validate_input(
                        self.hass,
                        state.settings,
                        api_token=state.login_token,
                        apply_modbus_config=state.apply_modbus_config,
                        claim_host=lambda: claim_host(state.settings),
                        allowed_host=state.allowed_host,
                    )
                except _SunSpecSwitchNeeded as needed:
                    return await self._async_ask_sunspec_switch(
                        needed,
                        state,
                        state.login_token,
                        minted=state.token_minted,
                        step_id=sunspec_step_id,
                    )
                minted = state.login_token if state.token_minted else None
                result = await self._async_finish_with_token(
                    on_success, state, info, minted
                )
                self._pending_flow_state = None
                return result
            except data_entry_flow.AbortFlow:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _set_form_error(errors, err)

        return await self._async_show_allowed_host_step(step_id=step_id, errors=errors)

    async def _async_log_in(
        self,
        state: _PendingFlowState,
        *,
        always_ask_password: bool = False,
        password_step_id: str,
        step_ids: tuple[str, str],
        claim_host: _HostClaim,
        on_success: _FlowFinishCallback,
    ):
        """Finish without the web API or with the stored token; ask for the password else."""
        settings = state.settings
        if settings[CONF_API_USERNAME] == WEB_API_DISABLED:
            info = await _validate_input(self.hass, settings)
            self._pending_flow_state = None
            return await on_success(settings, info, state.previous_host)
        token = await async_get_token_store(self.hass).async_load_token(
            settings[CONF_HOST], settings[CONF_API_USERNAME]
        )
        if token is None or always_ask_password:
            state.existing_token = token
            self._pending_flow_state = state
            return await self._async_show_password_step(step_id=password_step_id)
        try:
            return await self._async_use_stored_token(
                state,
                token,
                step_ids=step_ids,
                claim_host=claim_host,
                on_success=on_success,
            )
        except _InvalidApiCredentials:
            self._pending_flow_state = state
            return await self._async_show_password_step(step_id=password_step_id)

    async def _async_use_stored_token(
        self,
        state: _PendingFlowState,
        token: dict[str, str],
        *,
        step_ids: tuple[str, str],
        claim_host: _HostClaim,
        on_success: _FlowFinishCallback,
    ):
        """Check the inverter with the token held already, and finish."""
        try:
            info = await _validate_input(
                self.hass,
                state.settings,
                api_token=token,
                apply_modbus_config=state.apply_modbus_config,
                claim_host=lambda: claim_host(state.settings),
                ask_allowed_host=state.ask_allowed_host,
            )
        except (_SunSpecSwitchNeeded, _AllowedHostNeeded) as needed:
            return await self._async_ask_before_writing(
                needed, state, token, minted=False, step_ids=step_ids
            )
        self._pending_flow_state = None
        return await on_success(state.settings, info, state.previous_host)

    async def _async_handle_settings_step(
        self,
        *,
        user_input: dict[str, Any] | None,
        step_id: str,
        password_step_id: str,
        sunspec_step_id: str,
        allowed_host_step_id: str,
        defaults: dict[str, Any],
        previous_host: str | None,
        previous_settings: dict[str, Any] | None,
        force_apply_modbus_config: bool = False,
        always_ask_password: bool = False,
        claim_host: _HostClaim,
        on_success: _FlowFinishCallback,
        build_schema: Callable[[dict[str, Any]], vol.Schema] = settings_schema,
    ):
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                settings = _expand_settings_input(user_input, defaults)
                _validate_static_input(settings)
                # Before the login: validation may already write the Modbus
                # settings of a host another entry owns.
                await claim_host(settings)
                if (question := await self._async_look_at_device(settings)) is not None:
                    return question
                ask_allowed_host = asks_for_the_address(user_input, settings)
                # The question comes only with a write: Configure writes
                # unchanged settings only when asked to.
                apply_modbus_config = (
                    force_apply_modbus_config
                    or ask_allowed_host
                    or _should_apply_modbus_config(
                        settings,
                        previous_settings,
                    )
                )
                return await self._async_log_in(
                    _PendingFlowState(
                        settings,
                        previous_host,
                        apply_modbus_config,
                        ask_allowed_host=ask_allowed_host,
                    ),
                    always_ask_password=always_ask_password,
                    password_step_id=password_step_id,
                    step_ids=(sunspec_step_id, allowed_host_step_id),
                    claim_host=claim_host,
                    on_success=on_success,
                )
            except data_entry_flow.AbortFlow:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _set_form_error(errors, err)

        return self.async_show_form(
            step_id=step_id,
            data_schema=build_schema(defaults_after(errors, user_input, defaults)),
            errors=errors,
        )

    async def _async_handle_password_step(
        self,
        *,
        user_input: dict[str, Any] | None,
        step_id: str,
        sunspec_step_id: str,
        allowed_host_step_id: str,
        restart_step: _FlowRestartCallback,
        claim_host: _HostClaim,
        on_success: _FlowFinishCallback,
    ):
        errors: dict[str, str] = {}
        state = self._pending_flow_state
        if state is None:
            return await restart_step()

        if user_input is not None:
            try:
                # Again: another entry may have taken the host while this form
                # was open.
                await claim_host(state.settings)
                password = str(user_input.get(CONF_API_PASSWORD, "")).strip()
                username = state.settings[CONF_API_USERNAME]
                minted = not (password == "" and state.existing_token is not None)
                token = state.existing_token
                if minted:
                    token = await _async_mint_token(
                        self.hass,
                        state.settings[CONF_HOST],
                        password,
                        username,
                        on_datamanager=bool(state.settings.get(CONF_DATAMANAGER)),
                    )
                try:
                    info = await _validate_input(
                        self.hass,
                        state.settings,
                        api_token=token,
                        apply_modbus_config=state.apply_modbus_config,
                        claim_host=lambda: claim_host(state.settings),
                        ask_allowed_host=state.ask_allowed_host,
                    )
                except (_SunSpecSwitchNeeded, _AllowedHostNeeded) as needed:
                    return await self._async_ask_before_writing(
                        needed,
                        state,
                        token,
                        minted=minted,
                        step_ids=(sunspec_step_id, allowed_host_step_id),
                    )
                result = await self._async_finish_with_token(
                    on_success, state, info, token if minted else None
                )
                self._pending_flow_state = None
                return result
            except data_entry_flow.AbortFlow:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _set_form_error(errors, err)

        return await self._async_show_password_step(step_id=step_id, errors=errors)

    async def _async_finish_with_token(
        self,
        on_success: _FlowFinishCallback,
        state: _PendingFlowState,
        info: dict[str, Any],
        minted: dict[str, str] | None,
    ):
        """Keep a minted token only for a checked inverter and a finished flow.

        Saved first, a failed check or an aborted flow left a password-equivalent
        credential with no entry. The entry's setup reads it, so
        it is saved before the entry is created; if that fails, the token held
        before comes back: an aborted duplicate replaced the entry's own.
        A flow can fail after updating its own entry; that
        entry then logs in with the new token, which stays.
        """
        if minted is None:
            return await on_success(state.settings, info, state.previous_host)
        host = state.settings[CONF_HOST]
        username = state.settings[CONF_API_USERNAME]
        store = async_get_token_store(self.hass)
        previous = await store.async_load_token(host, username)
        try:
            await store.async_save_token(host, user=username, **minted)
            return await on_success(state.settings, info, state.previous_host)
        except BaseException:
            if not self._flow_entry_logs_in_with(host, username):
                await self._async_roll_back_token(host, username, previous)
            raise

    def _flow_entry(self) -> config_entries.ConfigEntry | None:
        """The entry this flow changes; none while it creates one."""
        raise NotImplementedError

    def _flow_entry_logs_in_with(self, host: str, username: str) -> bool:
        entry = self._flow_entry()
        if entry is None:
            return False
        settings = entry_defaults(entry)
        same_host = canonical_host(settings[CONF_HOST]) == canonical_host(host)
        return same_host and settings[CONF_API_USERNAME] == username

    async def _async_roll_back_token(
        self, host: str, username: str, previous: dict[str, str] | None
    ) -> None:
        """Put back what the store held before this flow's token.

        The token this flow minted goes, even when another entry uses the
        same host and role: that entry did not log in with it.
        """
        store = async_get_token_store(self.hass)
        if previous is None:
            await store.async_delete_token(host, username)
            return
        await store.async_save_token(host, user=username, **previous)


# The password step for a Datamanager's login, after the step it stands for.
SERVICE_LOGIN_STEP = "_service"


class ConfigFlow(NewInverterMixin, TokenFlowMixin, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1
    MINOR_VERSION = 14

    def __init__(self) -> None:
        self._pending_flow_state = None
        self._discovered_host = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FroniusModbusOptionsFlow()

    def _flow_entry(self) -> config_entries.ConfigEntry | None:
        sources = (config_entries.SOURCE_RECONFIGURE, config_entries.SOURCE_REAUTH)
        if self.source not in sources:
            return None
        return self.hass.config_entries.async_get_entry(self.context["entry_id"])

    async def _async_claim_reconfigured_host(self, settings: dict[str, Any]) -> None:
        await self._async_claim_entry_host(self._get_reconfigure_entry(), settings)

    async def _async_check_inverter(self, settings: dict[str, Any]) -> dict[str, Any]:
        return await _validate_input(self.hass, settings)

    async def _async_set_up_inverter(self, settings: dict[str, Any]):
        return await self._async_log_in(
            _PendingFlowState(settings, None, apply_modbus_config=False),
            password_step_id="user_password",
            step_ids=("user_sunspec", "user_allowed_host"),
            claim_host=self._async_claim_new_host,
            on_success=self._async_finish_user,
        )

    async def _async_finish_reconfigure(self, settings, info, previous_host):
        del info
        entry = self._get_reconfigure_entry()
        await async_update_entry_from_input(
            self.hass,
            entry,
            settings,
            previous_host=previous_host,
        )
        return self.async_abort(reason="reconfigure_successful")

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo):
        host = str(discovery_info.ip_address)
        serial = discovered_serial(discovery_info.properties)
        entry = entry_for_serial(self.hass, serial)
        if entry is not None and serial is not None:
            await async_follow_host(self.hass, entry, host, serial)
            return self.async_abort(reason="already_configured")
        # The web client builds its URLs from the bare host, which an IPv6
        # address cannot be.
        if discovery_info.ip_address.version != 4:
            return self.async_abort(reason="not_ipv4_address")
        await self._async_claim_unique_id({CONF_HOST: host, CONF_DATAMANAGER: False})
        self._discovered_host = host
        self.context["title_placeholders"] = {
            "name": discovered_model(discovery_info.name)
        }
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="user",
            password_step_id="user_password",
            sunspec_step_id="user_sunspec",
            allowed_host_step_id="user_allowed_host",
            defaults={**_default_payload(), CONF_HOST: self._discovered_host},
            previous_host=None,
            previous_settings=None,
            claim_host=self._async_claim_new_host,
            on_success=self._async_finish_user,
        )

    async def async_step_user_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="user_password",
            sunspec_step_id="user_sunspec",
            allowed_host_step_id="user_allowed_host",
            restart_step=self.async_step_user,
            claim_host=self._async_claim_new_host,
            on_success=self._async_finish_user,
        )

    async_step_user_password_service = async_step_user_password

    async def async_step_user_sunspec(self, user_input=None):
        return await self._async_handle_sunspec_step(
            user_input=user_input,
            step_id="user_sunspec",
            restart_step=self.async_step_user,
            claim_host=self._async_claim_new_host,
            on_success=self._async_finish_user,
        )

    async def async_step_user_allowed_host(self, user_input=None):
        return await self._async_handle_allowed_host_step(
            user_input=user_input,
            step_id="user_allowed_host",
            sunspec_step_id="user_sunspec",
            restart_step=self.async_step_user,
            claim_host=self._async_claim_new_host,
            on_success=self._async_finish_user,
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        defaults = entry_defaults(entry)
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="reconfigure",
            password_step_id="reconfigure_password",
            sunspec_step_id="reconfigure_sunspec",
            allowed_host_step_id="reconfigure_allowed_host",
            defaults=defaults,
            previous_host=defaults[CONF_HOST],
            previous_settings=defaults,
            force_apply_modbus_config=True,
            claim_host=self._async_claim_reconfigured_host,
            on_success=self._async_finish_reconfigure,
        )

    async def async_step_reconfigure_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="reconfigure_password",
            sunspec_step_id="reconfigure_sunspec",
            allowed_host_step_id="reconfigure_allowed_host",
            restart_step=self.async_step_reconfigure,
            claim_host=self._async_claim_reconfigured_host,
            on_success=self._async_finish_reconfigure,
        )

    async_step_reconfigure_password_service = async_step_reconfigure_password

    async def async_step_reconfigure_sunspec(self, user_input=None):
        return await self._async_handle_sunspec_step(
            user_input=user_input,
            step_id="reconfigure_sunspec",
            restart_step=self.async_step_reconfigure,
            claim_host=self._async_claim_reconfigured_host,
            on_success=self._async_finish_reconfigure,
        )

    async def async_step_reconfigure_allowed_host(self, user_input=None):
        return await self._async_handle_allowed_host_step(
            user_input=user_input,
            step_id="reconfigure_allowed_host",
            sunspec_step_id="reconfigure_sunspec",
            restart_step=self.async_step_reconfigure,
            claim_host=self._async_claim_reconfigured_host,
            on_success=self._async_finish_reconfigure,
        )

    async def _async_claim_reauth_host(self, settings: dict[str, Any]) -> None:
        entry = self._flow_entry()
        if entry is None:
            raise data_entry_flow.AbortFlow("entry_not_found")
        await self._async_claim_entry_host(entry, settings)

    async def _async_finish_reauth(self, settings, info, previous_host):
        del info
        entry = self._flow_entry()
        if entry is None:
            # An abort, not a success: the token minted for the gone entry is
            # then taken back.
            raise data_entry_flow.AbortFlow("entry_not_found")
        # The form asked for the role alone: the rest is what the entry holds
        # now, not what it held when the flow began.
        current = entry_defaults(entry)
        current[CONF_API_USERNAME] = settings[CONF_API_USERNAME]
        if CONF_DATAMANAGER in settings:
            current[CONF_DATAMANAGER] = settings[CONF_DATAMANAGER]
        await async_update_entry_from_input(
            self.hass, entry, current, previous_host=previous_host
        )
        return self.async_abort(reason="reauth_successful")

    async def async_step_reauth(self, entry_data):
        del entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        defaults = entry_defaults(self._get_reauth_entry())
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="reauth_confirm",
            password_step_id="reauth_password",
            sunspec_step_id="reauth_sunspec",
            allowed_host_step_id="reauth_allowed_host",
            defaults=defaults,
            previous_host=defaults[CONF_HOST],
            previous_settings=defaults,
            force_apply_modbus_config=True,
            claim_host=self._async_claim_reauth_host,
            on_success=self._async_finish_reauth,
            build_schema=role_schema,
        )

    async def async_step_reauth_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="reauth_password",
            sunspec_step_id="reauth_sunspec",
            allowed_host_step_id="reauth_allowed_host",
            restart_step=self.async_step_reauth_confirm,
            claim_host=self._async_claim_reauth_host,
            on_success=self._async_finish_reauth,
        )

    async_step_reauth_password_service = async_step_reauth_password

    async def async_step_reauth_sunspec(self, user_input=None):
        return await self._async_handle_sunspec_step(
            user_input=user_input,
            step_id="reauth_sunspec",
            restart_step=self.async_step_reauth_confirm,
            claim_host=self._async_claim_reauth_host,
            on_success=self._async_finish_reauth,
        )

    async def async_step_reauth_allowed_host(self, user_input=None):
        return await self._async_handle_allowed_host_step(
            user_input=user_input,
            step_id="reauth_allowed_host",
            sunspec_step_id="reauth_sunspec",
            restart_step=self.async_step_reauth_confirm,
            claim_host=self._async_claim_reauth_host,
            on_success=self._async_finish_reauth,
        )


class FroniusModbusOptionsFlow(TokenFlowMixin, config_entries.OptionsFlow):
    """Handle Fronius Modbus options."""

    def _flow_entry(self) -> config_entries.ConfigEntry | None:
        return self.config_entry

    async def _async_claim_host(self, settings: dict[str, Any]) -> None:
        await self._async_claim_entry_host(self.config_entry, settings)

    async def _async_finish_options(self, settings, info, previous_host):
        del info
        unique_id = _claim_host(self.hass, self.config_entry, settings)
        options = _entry_payload(settings, reconfigure_required=False)
        # Read before the update: its listener starts the reload right away.
        loaded = self.config_entry.state is config_entries.ConfigEntryState.LOADED
        # The options land before the tokens are sorted out: the old host or
        # role must already be released when the unused ones are looked up.
        changed = self.hass.config_entries.async_update_entry(
            self.config_entry,
            unique_id=unique_id,
            title=entry_title_after(
                self.config_entry.title,
                {**self.config_entry.data, **self.config_entry.options},
                settings,
            ),
            data={
                **self.config_entry.data,
                **device_answer(self.config_entry, settings),
            },
            options=options,
        )
        await async_forget_unused_tokens(self.hass, previous_host)
        await _async_reload_unless_listened(
            self.hass, self.config_entry, changed=changed, loaded=loaded
        )
        return self.async_create_entry(title="", data=options)

    async def async_step_init(self, user_input=None):
        defaults = entry_defaults(self.config_entry)
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="init",
            password_step_id="password",
            sunspec_step_id="sunspec",
            allowed_host_step_id="allowed_host",
            defaults=defaults,
            previous_host=defaults[CONF_HOST],
            previous_settings=defaults,
            # Configure is the one place to add or replace the passwords, so
            # the password step is always offered here.
            always_ask_password=True,
            claim_host=self._async_claim_host,
            on_success=self._async_finish_options,
        )

    async def async_step_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="password",
            sunspec_step_id="sunspec",
            allowed_host_step_id="allowed_host",
            restart_step=self.async_step_init,
            claim_host=self._async_claim_host,
            on_success=self._async_finish_options,
        )

    async_step_password_service = async_step_password

    async def async_step_sunspec(self, user_input=None):
        return await self._async_handle_sunspec_step(
            user_input=user_input,
            step_id="sunspec",
            restart_step=self.async_step_init,
            claim_host=self._async_claim_host,
            on_success=self._async_finish_options,
        )

    async def async_step_allowed_host(self, user_input=None):
        return await self._async_handle_allowed_host_step(
            user_input=user_input,
            step_id="allowed_host",
            sunspec_step_id="sunspec",
            restart_step=self.async_step_init,
            claim_host=self._async_claim_host,
            on_success=self._async_finish_options,
        )
