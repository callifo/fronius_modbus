from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from modbus_connection import ModbusError, ModbusTcpParams
import voluptuous as vol

from homeassistant import config_entries, data_entry_flow, exceptions
from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_AUTO_ENABLE_MODBUS,
    CONF_INVERTER_UNIT_ID,
    CONF_RECONFIGURE_REQUIRED,
    CONF_MODBUS_RESTRICTION,
    CONF_WEB_SCAN_INTERVAL,
    DEFAULT_AUTO_ENABLE_MODBUS,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_METER_UNIT_ID,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEB_SCAN_INTERVAL,
    DOMAIN,
    MINIMUM_SCAN_INTERVAL,
    API_USERNAME,
    API_USERNAMES,
    SUPPORTED_MANUFACTURERS,
    SUPPORTED_MODELS,
    WEB_API_DISABLED,
    ModbusRestriction,
    entry_title,
    entry_unique_id,
)
from .discovery import (
    async_follow_host,
    discovered_model,
    discovered_serial,
    entry_for_serial,
)
from .fronius_modbus_api.device import FroniusInverter
from .froniuswebclient import (
    ClientIpResolutionError,
    FroniusWebClient,
    FroniusWebResponseError,
    mint_token,
)
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


@dataclass(slots=True)
class _PendingFlowState:
    settings: dict[str, Any]
    previous_host: str | None
    apply_modbus_config: bool
    # Set when the password step is shown although a token exists (Configure):
    # an empty password then keeps this token (audit F11).
    existing_token: dict[str, str] | None = None


class _CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class _CannotConnectModbus(exceptions.HomeAssistantError):
    """Modbus did not answer, and without the web API nothing switched it on."""


class _InvalidHost(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid hostname."""


class _InvalidPort(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid port."""


class _UnsupportedHardware(exceptions.HomeAssistantError):
    """Error to indicate there is unsupported hardware."""


class _AddressesNotUnique(exceptions.HomeAssistantError):
    """Error to indicate that the modbus addresses are not unique."""


class _AlreadyConfigured(exceptions.HomeAssistantError):
    """Another entry already serves the host being configured."""


class _ScanIntervalTooShort(exceptions.HomeAssistantError):
    """Error to indicate the scan interval is too short."""


class _MissingApiPassword(exceptions.HomeAssistantError):
    """Error to indicate the Web API password is required."""


class _InvalidApiCredentials(exceptions.HomeAssistantError):
    """Error to indicate Fronius web API credentials are invalid."""


class _CannotResolveLocalIp(exceptions.HomeAssistantError):
    """Error to indicate the local IP for Modbus restriction cannot be resolved."""


def _default_payload() -> dict[str, Any]:
    return {
        CONF_NAME: DEFAULT_NAME,
        CONF_HOST: "",
        CONF_PORT: DEFAULT_PORT,
        CONF_INVERTER_UNIT_ID: DEFAULT_INVERTER_UNIT_ID,
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        CONF_WEB_SCAN_INTERVAL: DEFAULT_WEB_SCAN_INTERVAL,
        CONF_API_USERNAME: API_USERNAME,
        CONF_AUTO_ENABLE_MODBUS: DEFAULT_AUTO_ENABLE_MODBUS,
        CONF_MODBUS_RESTRICTION: ModbusRestriction.KEEP,
    }


def _expand_settings_input(
    user_input: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _default_payload()
    if defaults:
        payload.update(defaults)
    payload[CONF_HOST] = str(user_input.get(CONF_HOST, payload[CONF_HOST])).strip()
    payload[CONF_SCAN_INTERVAL] = int(
        user_input.get(CONF_SCAN_INTERVAL, payload[CONF_SCAN_INTERVAL])
    )
    payload[CONF_MODBUS_RESTRICTION] = ModbusRestriction(
        user_input.get(CONF_MODBUS_RESTRICTION, payload[CONF_MODBUS_RESTRICTION])
    )
    payload[CONF_WEB_SCAN_INTERVAL] = int(
        user_input.get(CONF_WEB_SCAN_INTERVAL, payload[CONF_WEB_SCAN_INTERVAL])
    )
    username = (
        str(user_input.get(CONF_API_USERNAME, payload[CONF_API_USERNAME]))
        .strip()
        .lower()
    )
    choices = (*API_USERNAMES, WEB_API_DISABLED)
    payload[CONF_API_USERNAME] = username if username in choices else API_USERNAME
    payload.pop(CONF_API_PASSWORD, None)
    payload.pop("meter_modbus_unit_id", None)
    payload.pop("meter_modbus_unit_ids", None)
    return payload


def _entry_payload(
    data: dict[str, Any], *, reconfigure_required: bool
) -> dict[str, Any]:
    payload = dict(data)
    payload.pop(CONF_API_PASSWORD, None)
    payload.pop("meter_modbus_unit_id", None)
    payload.pop("meter_modbus_unit_ids", None)
    payload[CONF_RECONFIGURE_REQUIRED] = reconfigure_required
    return payload


def entry_defaults(entry: config_entries.ConfigEntry) -> dict[str, Any]:
    defaults = {**entry.data, **entry.options}
    try:
        defaults[CONF_SCAN_INTERVAL] = int(
            defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
    except TypeError, ValueError:
        defaults[CONF_SCAN_INTERVAL] = DEFAULT_SCAN_INTERVAL
    return _expand_settings_input({}, defaults)


def _build_settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.Coerce(int),
            vol.Required(
                CONF_WEB_SCAN_INTERVAL,
                default=defaults.get(CONF_WEB_SCAN_INTERVAL, DEFAULT_WEB_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=MINIMUM_SCAN_INTERVAL, max=3600)),
            vol.Required(
                CONF_API_USERNAME,
                default=defaults.get(CONF_API_USERNAME, API_USERNAME),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[*API_USERNAMES, WEB_API_DISABLED],
                    mode=SelectSelectorMode.LIST,
                    translation_key="api_username",
                )
            ),
            vol.Required(
                CONF_MODBUS_RESTRICTION,
                default=defaults.get(CONF_MODBUS_RESTRICTION, ModbusRestriction.KEEP),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(ModbusRestriction),
                    mode=SelectSelectorMode.LIST,
                    translation_key=CONF_MODBUS_RESTRICTION,
                )
            ),
        }
    )


def _build_password_schema(*, keep_stored: bool = False) -> vol.Schema:
    password_field = (
        vol.Optional(CONF_API_PASSWORD, default="")
        if keep_stored
        else vol.Required(CONF_API_PASSWORD)
    )
    return vol.Schema(
        {
            password_field: TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            ),
        }
    )


def _set_form_error(errors: dict[str, str], err: Exception) -> None:
    if isinstance(err, _CannotConnect):
        errors["base"] = "cannot_connect"
    elif isinstance(err, _CannotConnectModbus):
        errors["base"] = "cannot_connect_modbus"
    elif isinstance(err, _InvalidPort):
        errors["base"] = "invalid_port"
    elif isinstance(err, _InvalidHost):
        errors["host"] = "invalid_host"
    elif isinstance(err, _ScanIntervalTooShort):
        errors["base"] = "scan_interval_too_short"
    elif isinstance(err, _MissingApiPassword):
        errors["base"] = "missing_api_password"
    elif isinstance(err, _InvalidApiCredentials):
        errors["base"] = "invalid_api_credentials"
    elif isinstance(err, _CannotResolveLocalIp):
        errors["base"] = "cannot_resolve_local_ip"
    elif isinstance(err, _UnsupportedHardware):
        errors["base"] = "unsupported_hardware"
    elif isinstance(err, _AddressesNotUnique):
        errors["base"] = "modbus_address_conflict"
    elif isinstance(err, _AlreadyConfigured):
        errors["base"] = "already_configured"
    else:
        _LOGGER.exception("Unexpected exception")
        errors["base"] = "unknown"


def _validate_static_input(data: dict[str, Any]) -> None:
    if len(data[CONF_HOST]) < 3:
        raise _InvalidHost
    if data[CONF_PORT] > 65535:
        raise _InvalidPort
    if data[CONF_SCAN_INTERVAL] < MINIMUM_SCAN_INTERVAL:
        raise _ScanIntervalTooShort

    all_addresses = [DEFAULT_METER_UNIT_ID, data[CONF_INVERTER_UNIT_ID]]
    if len(all_addresses) > len(set(all_addresses)):
        _LOGGER.error("Modbus addresses are not unique %s", all_addresses)
        raise _AddressesNotUnique


def _should_apply_modbus_config(
    settings: dict[str, Any],
    previous_settings: dict[str, Any] | None,
) -> bool:
    if previous_settings is None:
        return True
    # Without a login nothing was written, so the saved choices were never
    # applied to the inverter (audit R6D-01).
    if previous_settings.get(CONF_API_USERNAME) == WEB_API_DISABLED:
        return True

    return (
        settings[CONF_HOST] != previous_settings.get(CONF_HOST, "")
        or settings[CONF_PORT] != previous_settings.get(CONF_PORT, DEFAULT_PORT)
        or settings[CONF_INVERTER_UNIT_ID]
        != previous_settings.get(CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID)
        or settings[CONF_MODBUS_RESTRICTION]
        != previous_settings.get(
            CONF_MODBUS_RESTRICTION,
            ModbusRestriction.KEEP,
        )
    )


async def _async_load_token(
    hass: HomeAssistant, host: str, username: str
) -> dict[str, str] | None:
    return await async_get_token_store(hass).async_load_token(host, username)


async def _async_save_token(
    hass: HomeAssistant, host: str, username: str, token: dict[str, str]
) -> None:
    await async_get_token_store(hass).async_save_token(
        host,
        realm=token["realm"],
        token=token["token"],
        user=username,
    )


async def _async_mint_token(
    hass: HomeAssistant,
    host: str,
    password: str,
    username: str = API_USERNAME,
) -> dict[str, str]:
    password = str(password).strip()
    if password == "":
        raise _MissingApiPassword

    try:
        token = await hass.async_add_executor_job(
            mint_token,
            host,
            username,
            password,
        )
    except Exception as err:
        raise _CannotConnect from err

    if not token:
        raise _InvalidApiCredentials
    return token


async def _async_prepare_web_api(
    hass: HomeAssistant,
    data: dict[str, Any],
    api_password: str,
    api_token: dict[str, str] | None,
    apply_modbus_config: bool,
    claim_host: Callable[[], Awaitable[None]] | None,
) -> None:
    """Log in, and switch Modbus on where the flow asks for it.

    ``claim_host`` runs right before the Modbus settings are written: another
    entry can take the host while the token is minted or the login runs, and
    the settings would then change on an inverter this flow does not own
    (audit R730-01).
    """
    client = FroniusWebClient(
        host=data[CONF_HOST],
        username=data[CONF_API_USERNAME],
        password=api_password or "",
        token=api_token,
    )
    if not await hass.async_add_executor_job(client.login):
        raise _InvalidApiCredentials
    enable_modbus = data.get(CONF_AUTO_ENABLE_MODBUS, DEFAULT_AUTO_ENABLE_MODBUS)
    if not (apply_modbus_config and enable_modbus):
        return
    if claim_host is not None:
        await claim_host()
    await hass.async_add_executor_job(
        client.ensure_modbus_enabled,
        data[CONF_PORT],
        DEFAULT_METER_UNIT_ID,
        data[CONF_INVERTER_UNIT_ID],
        data[CONF_MODBUS_RESTRICTION],
    )
    # The inverter restarts its Modbus server after the settings write.
    await asyncio.sleep(1.0)


async def _validate_input(
    hass: HomeAssistant,
    data: dict[str, Any],
    *,
    api_password: str = "",
    api_token: dict[str, str] | None = None,
    apply_modbus_config: bool = False,
    claim_host: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    _validate_static_input(data)

    web_api = data[CONF_API_USERNAME] != WEB_API_DISABLED
    if web_api and not api_password and api_token is None:
        raise _MissingApiPassword

    try:
        if web_api:
            await _async_prepare_web_api(
                hass, data, api_password, api_token, apply_modbus_config, claim_host
            )
        async with async_get_temporary_unit(
            hass,
            ModbusTcpParams(host=data[CONF_HOST], port=data[CONF_PORT]),
            data[CONF_INVERTER_UNIT_ID],
        ) as unit:
            identity = await FroniusInverter.async_probe(unit)
    except ClientIpResolutionError as err:
        raise _CannotResolveLocalIp from err
    except _InvalidApiCredentials, _AlreadyConfigured, data_entry_flow.AbortFlow:
        raise
    except (
        ModbusError,
        HomeAssistantError,
        FroniusWebResponseError,
        OSError,
        TimeoutError,
    ) as err:
        _LOGGER.error("Cannot reach inverter: %s", err)
        if not web_api:
            raise _CannotConnectModbus from err
        raise _CannotConnect from err

    if identity.manufacturer not in SUPPORTED_MANUFACTURERS:
        _LOGGER.error("Unsupported manufacturer: %r", identity.manufacturer)
        raise _UnsupportedHardware
    if not any(identity.model.startswith(model) for model in SUPPORTED_MODELS):
        _LOGGER.warning("Untested model %s", identity.model)

    return {"title": entry_title(data)}


def _claim_host(
    hass: HomeAssistant, entry: config_entries.ConfigEntry, settings: dict[str, Any]
) -> str:
    """The unique id for ``settings``' host, unless another entry already holds it.

    The unique id is the host; a reconfigure or options change that moves the
    host has to move the id with it, or duplicate detection keeps guarding the
    old host and lets a second entry for the new one through (audit F12).
    """
    unique_id = entry_unique_id(settings)
    holder = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    if holder is not None and holder.entry_id != entry.entry_id:
        raise _AlreadyConfigured
    return unique_id


async def async_update_entry_from_input(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    validated_input: dict[str, Any],
    *,
    previous_host: str | None = None,
) -> None:
    unique_id = _claim_host(hass, entry, validated_input)
    updated_payload = _entry_payload(validated_input, reconfigure_required=False)
    new_data = {**entry.data, **updated_payload}
    new_options = {**entry.options, **updated_payload}
    new_data.pop(CONF_API_PASSWORD, None)
    new_options.pop(CONF_API_PASSWORD, None)
    new_data.pop("meter_modbus_unit_id", None)
    new_options.pop("meter_modbus_unit_id", None)
    new_data.pop("meter_modbus_unit_ids", None)
    new_options.pop("meter_modbus_unit_ids", None)
    # Read before the update: its listener starts the reload right away.
    loaded = entry.state is config_entries.ConfigEntryState.LOADED
    changed = hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        title=entry_title(validated_input),
        unique_id=unique_id,
    )
    if previous_host:
        await async_forget_unused_tokens(hass, previous_host)
    # A change to a loaded entry reloads it through its update listener; a
    # second call here reloaded it twice (audit F24-10). A new token alone
    # changes nothing in the entry, so that still needs the call.
    if changed and loaded:
        return
    await hass.config_entries.async_reload(entry.entry_id)


class TokenFlowMixin:
    _pending_flow_state: _PendingFlowState | None = None
    hass: HomeAssistant

    async def _async_claim_entry_host(
        self, entry: config_entries.ConfigEntry, settings: dict[str, Any]
    ) -> None:
        _claim_host(self.hass, entry, settings)

    async def _async_show_password_step(
        self,
        *,
        step_id: str,
        errors: dict[str, str] | None = None,
    ):
        state = self._pending_flow_state
        placeholders = None
        if state is not None:
            placeholders = {
                "entry_title": entry_title(state.settings),
                "host": str(state.settings.get(CONF_HOST, "")),
            }
        return self.async_show_form(
            step_id=step_id,
            data_schema=_build_password_schema(
                keep_stored=state is not None and state.existing_token is not None
            ),
            errors=errors or {},
            description_placeholders=placeholders,
        )

    async def _async_handle_settings_step(
        self,
        *,
        user_input: dict[str, Any] | None,
        step_id: str,
        password_step_id: str,
        defaults: dict[str, Any],
        previous_host: str | None,
        previous_settings: dict[str, Any] | None,
        force_apply_modbus_config: bool = False,
        always_ask_password: bool = False,
        claim_host: _HostClaim,
        on_success: _FlowFinishCallback,
    ):
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                settings = _expand_settings_input(user_input, defaults)
                _validate_static_input(settings)
                # Before the login: validation may already write the Modbus
                # settings of a host another entry owns (audit FA0FB-02).
                await claim_host(settings)
                apply_modbus_config = (
                    force_apply_modbus_config
                    or _should_apply_modbus_config(
                        settings,
                        previous_settings,
                    )
                )
                if settings[CONF_API_USERNAME] == WEB_API_DISABLED:
                    info = await _validate_input(self.hass, settings)
                    self._pending_flow_state = None
                    return await on_success(settings, info, previous_host)
                token = await _async_load_token(
                    self.hass, settings[CONF_HOST], settings[CONF_API_USERNAME]
                )
                if token is None or always_ask_password:
                    self._pending_flow_state = _PendingFlowState(
                        settings,
                        previous_host,
                        apply_modbus_config,
                        existing_token=token,
                    )
                    return await self._async_show_password_step(
                        step_id=password_step_id
                    )

                info = await _validate_input(
                    self.hass,
                    settings,
                    api_token=token,
                    apply_modbus_config=apply_modbus_config,
                    claim_host=lambda: claim_host(settings),
                )
                self._pending_flow_state = None
                return await on_success(settings, info, previous_host)
            except data_entry_flow.AbortFlow:
                raise
            except _InvalidApiCredentials:
                self._pending_flow_state = _PendingFlowState(
                    settings,
                    previous_host,
                    apply_modbus_config,
                )
                return await self._async_show_password_step(step_id=password_step_id)
            except Exception as err:  # pylint: disable=broad-except
                _set_form_error(errors, err)

        return self.async_show_form(
            step_id=step_id,
            data_schema=_build_settings_schema(defaults),
            errors=errors,
        )

    async def _async_handle_password_step(
        self,
        *,
        user_input: dict[str, Any] | None,
        step_id: str,
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
                # was open (audit RR770-01).
                await claim_host(state.settings)
                password = str(user_input.get(CONF_API_PASSWORD, "")).strip()
                username = state.settings[CONF_API_USERNAME]
                minted = not (password == "" and state.existing_token is not None)
                token = state.existing_token
                if minted:
                    token = await _async_mint_token(
                        self.hass, state.settings[CONF_HOST], password, username
                    )
                info = await _validate_input(
                    self.hass,
                    state.settings,
                    api_token=token,
                    apply_modbus_config=state.apply_modbus_config,
                    claim_host=lambda: claim_host(state.settings),
                )
                self._pending_flow_state = None
                return await self._async_finish_with_token(
                    on_success, state, info, token if minted else None
                )
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
        credential with no entry (audit RA24-01). The entry's setup reads it, so
        it is saved before the entry is created; if that fails, the token held
        before comes back: an aborted duplicate replaced the entry's own
        (reaudit RE26-04). A flow can fail after updating its own entry; that
        entry then logs in with the new token, which stays (R26-02, D8AE-02).
        """
        if minted is None:
            return await on_success(state.settings, info, state.previous_host)
        host = state.settings[CONF_HOST]
        username = state.settings[CONF_API_USERNAME]
        store = async_get_token_store(self.hass)
        previous = await store.async_load_token(host, username)
        try:
            await _async_save_token(self.hass, host, username, minted)
            return await on_success(state.settings, info, state.previous_host)
        except BaseException:
            if not self._flow_entry_logs_in_with(host, username):
                await self._async_roll_back_token(host, username, previous)
            raise

    def _flow_entry(self) -> config_entries.ConfigEntry | None:
        """The entry this flow changes; none while it creates one."""
        return None

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
        same host and role: that entry did not log in with it (audit RR770-02).
        """
        store = async_get_token_store(self.hass)
        if previous is None:
            await store.async_delete_token(host, username)
            return
        await _async_save_token(self.hass, host, username, previous)


class ConfigFlow(TokenFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1
    MINOR_VERSION = 14
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        self._pending_flow_state = None
        self._discovered_host = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FroniusModbusOptionsFlow()

    def _flow_entry(self) -> config_entries.ConfigEntry | None:
        if self.source != config_entries.SOURCE_RECONFIGURE:
            return None
        return self._get_reconfigure_entry()

    async def _async_claim_new_host(self, settings: dict[str, Any]) -> None:
        unique_id = entry_unique_id(settings)
        # A card for this host gives way to the owner adding it by hand; two
        # flows adding it by hand still stop each other (reaudit Z-01).
        for card in self._async_in_progress(match_context={"unique_id": unique_id}):
            if card["context"]["source"] == config_entries.SOURCE_ZEROCONF:
                self.hass.config_entries.flow.async_abort(card["flow_id"])
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

    async def _async_claim_reconfigured_host(self, settings: dict[str, Any]) -> None:
        await self._async_claim_entry_host(self._get_reconfigure_entry(), settings)

    async def _async_finish_user(self, settings, info, previous_host):
        del previous_host
        await self.async_set_unique_id(entry_unique_id(settings))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=info["title"],
            data=_entry_payload(settings, reconfigure_required=False),
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
        entry = entry_for_serial(
            self.hass, discovered_serial(discovery_info.properties)
        )
        if entry is not None:
            await async_follow_host(self.hass, entry, host)
            return self.async_abort(reason="already_configured")
        # The web client builds its URLs from the bare host, which an IPv6
        # address cannot be (audit R3B-03).
        if discovery_info.ip_address.version != 4:
            return self.async_abort(reason="not_ipv4_address")
        await self.async_set_unique_id(entry_unique_id({CONF_HOST: host}))
        self._abort_if_unique_id_configured()
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
            defaults={**_default_payload(), CONF_HOST: self._discovered_host},
            previous_host=None,
            previous_settings=None,
            force_apply_modbus_config=True,
            claim_host=self._async_claim_new_host,
            on_success=self._async_finish_user,
        )

    async def async_step_user_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="user_password",
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
            restart_step=self.async_step_reconfigure,
            claim_host=self._async_claim_reconfigured_host,
            on_success=self._async_finish_reconfigure,
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
        # The options land before the tokens are sorted out: the old host or
        # role must already be released when the unused ones are looked up.
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            unique_id=unique_id,
            title=entry_title(settings),
            options=options,
        )
        await async_forget_unused_tokens(self.hass, previous_host)
        return self.async_create_entry(title="", data=options)

    async def async_step_init(self, user_input=None):
        defaults = entry_defaults(self.config_entry)
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="init",
            password_step_id="password",
            defaults=defaults,
            previous_host=defaults[CONF_HOST],
            previous_settings=defaults,
            # Configure is the one place to add or replace the passwords, so
            # the password step is always offered here (audit F11).
            always_ask_password=True,
            claim_host=self._async_claim_host,
            on_success=self._async_finish_options,
        )

    async def async_step_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="password",
            restart_step=self.async_step_init,
            claim_host=self._async_claim_host,
            on_success=self._async_finish_options,
        )
