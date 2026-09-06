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
    CONF_RESTRICT_MODBUS_TO_THIS_IP,
    CONF_WEB_SCAN_INTERVAL,
    DEFAULT_AUTO_ENABLE_MODBUS,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_METER_UNIT_ID,
    DEFAULT_METER_UNIT_IDS,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEB_SCAN_INTERVAL,
    DOMAIN,
    MINIMUM_SCAN_INTERVAL,
    API_USERNAME,
    API_USERNAMES,
    TECHNICIAN_USERNAME,
    SUPPORTED_MANUFACTURERS,
    SUPPORTED_MODELS,
)
from .fronius_modbus_api.device import FroniusInverter
from .froniuswebclient import ClientIpResolutionError, FroniusWebClient, mint_token
from .token_store import async_get_token_store

_LOGGER = logging.getLogger(__name__)

type _FlowFinishCallback = Callable[
    [dict[str, Any], dict[str, Any], str | None],
    Awaitable[Any],
]
type _FlowRestartCallback = Callable[[], Awaitable[Any]]


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
        CONF_RESTRICT_MODBUS_TO_THIS_IP: DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
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
    payload[CONF_RESTRICT_MODBUS_TO_THIS_IP] = bool(
        user_input.get(
            CONF_RESTRICT_MODBUS_TO_THIS_IP,
            payload[CONF_RESTRICT_MODBUS_TO_THIS_IP],
        )
    )
    payload[CONF_WEB_SCAN_INTERVAL] = int(
        user_input.get(CONF_WEB_SCAN_INTERVAL, payload[CONF_WEB_SCAN_INTERVAL])
    )
    username = (
        str(user_input.get(CONF_API_USERNAME, payload[CONF_API_USERNAME]))
        .strip()
        .lower()
    )
    payload[CONF_API_USERNAME] = username if username in API_USERNAMES else API_USERNAME
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


def _entry_title(data: dict[str, Any]) -> str:
    host = str(data.get(CONF_HOST, "")).strip()
    name = str(data.get(CONF_NAME, DEFAULT_NAME)).strip() or DEFAULT_NAME
    return f"{name} {host}" if host else name


def _entry_unique_id(data: dict[str, Any]) -> str:
    return str(data.get(CONF_HOST, "")).strip().lower()


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
                    options=list(API_USERNAMES),
                    mode=SelectSelectorMode.LIST,
                    translation_key="api_username",
                )
            ),
            vol.Required(
                CONF_RESTRICT_MODBUS_TO_THIS_IP,
                default=defaults.get(
                    CONF_RESTRICT_MODBUS_TO_THIS_IP,
                    DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
                ),
            ): bool,
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

    all_addresses = [DEFAULT_METER_UNIT_IDS[0], data[CONF_INVERTER_UNIT_ID]]
    if len(all_addresses) > len(set(all_addresses)):
        _LOGGER.error("Modbus addresses are not unique %s", all_addresses)
        raise _AddressesNotUnique


def _should_apply_modbus_config(
    settings: dict[str, Any],
    previous_settings: dict[str, Any] | None,
) -> bool:
    if previous_settings is None:
        return True

    return (
        settings[CONF_HOST] != previous_settings.get(CONF_HOST, "")
        or settings[CONF_PORT] != previous_settings.get(CONF_PORT, DEFAULT_PORT)
        or settings[CONF_INVERTER_UNIT_ID]
        != previous_settings.get(CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID)
        or settings[CONF_RESTRICT_MODBUS_TO_THIS_IP]
        != previous_settings.get(
            CONF_RESTRICT_MODBUS_TO_THIS_IP,
            DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
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


async def _async_delete_token(hass: HomeAssistant, host: str | None) -> None:
    if host:
        token_store = async_get_token_store(hass)
        await token_store.async_delete_token(host, API_USERNAME)
        await token_store.async_delete_token(host, TECHNICIAN_USERNAME)


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


async def _validate_input(
    hass: HomeAssistant,
    data: dict[str, Any],
    *,
    api_password: str = "",
    api_token: dict[str, str] | None = None,
    apply_modbus_config: bool = False,
) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    _validate_static_input(data)

    if not api_password and api_token is None:
        raise _MissingApiPassword

    client = FroniusWebClient(
        host=data[CONF_HOST],
        username=data[CONF_API_USERNAME],
        password=api_password or "",
        token=api_token,
    )
    try:
        if not await hass.async_add_executor_job(client.login):
            raise _InvalidApiCredentials
        if apply_modbus_config and data.get(
            CONF_AUTO_ENABLE_MODBUS, DEFAULT_AUTO_ENABLE_MODBUS
        ):
            await hass.async_add_executor_job(
                client.ensure_modbus_enabled,
                data[CONF_PORT],
                DEFAULT_METER_UNIT_ID,
                data[CONF_INVERTER_UNIT_ID],
                data[CONF_RESTRICT_MODBUS_TO_THIS_IP],
            )
            # The inverter restarts its Modbus server after the settings write.
            await asyncio.sleep(1.0)
        async with async_get_temporary_unit(
            hass,
            ModbusTcpParams(host=data[CONF_HOST], port=data[CONF_PORT]),
            data[CONF_INVERTER_UNIT_ID],
        ) as unit:
            identity = await FroniusInverter.async_probe(unit)
    except ClientIpResolutionError as err:
        raise _CannotResolveLocalIp from err
    except _InvalidApiCredentials:
        raise
    except (ModbusError, HomeAssistantError, OSError, TimeoutError) as err:
        _LOGGER.error("Cannot reach inverter: %s", err)
        raise _CannotConnect from err

    if identity.manufacturer not in SUPPORTED_MANUFACTURERS:
        _LOGGER.error("Unsupported manufacturer: %r", identity.manufacturer)
        raise _UnsupportedHardware
    if not any(identity.model.startswith(model) for model in SUPPORTED_MODELS):
        _LOGGER.warning("Untested model %s", identity.model)

    return {"title": _entry_title(data)}


def _claim_host(
    hass: HomeAssistant, entry: config_entries.ConfigEntry, settings: dict[str, Any]
) -> str:
    """The unique id for ``settings``' host, unless another entry already holds it.

    The unique id is the host; a reconfigure or options change that moves the
    host has to move the id with it, or duplicate detection keeps guarding the
    old host and lets a second entry for the new one through (audit F12).
    """
    unique_id = _entry_unique_id(settings)
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
    hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        title=_entry_title(validated_input),
        unique_id=unique_id,
    )
    if previous_host and previous_host != validated_input[CONF_HOST]:
        await _async_delete_token(hass, previous_host)
    await hass.config_entries.async_reload(entry.entry_id)


class TokenFlowMixin:
    _pending_flow_state: _PendingFlowState | None = None

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
                "entry_title": _entry_title(state.settings),
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
        on_success: _FlowFinishCallback,
    ):
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                settings = _expand_settings_input(user_input, defaults)
                _validate_static_input(settings)
                apply_modbus_config = (
                    force_apply_modbus_config
                    or _should_apply_modbus_config(
                        settings,
                        previous_settings,
                    )
                )
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
        on_success: _FlowFinishCallback,
    ):
        errors: dict[str, str] = {}
        state = self._pending_flow_state
        if state is None:
            return await restart_step()

        if user_input is not None:
            try:
                password = str(user_input.get(CONF_API_PASSWORD, "")).strip()
                username = state.settings[CONF_API_USERNAME]
                if password == "" and state.existing_token is not None:
                    token = state.existing_token
                else:
                    token = await _async_mint_token(
                        self.hass, state.settings[CONF_HOST], password, username
                    )
                    await _async_save_token(
                        self.hass, state.settings[CONF_HOST], username, token
                    )
                info = await _validate_input(
                    self.hass,
                    state.settings,
                    api_token=token,
                    apply_modbus_config=state.apply_modbus_config,
                )
                self._pending_flow_state = None
                return await on_success(state.settings, info, state.previous_host)
            except data_entry_flow.AbortFlow:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _set_form_error(errors, err)

        return await self._async_show_password_step(step_id=step_id, errors=errors)


class ConfigFlow(TokenFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1
    MINOR_VERSION = 11
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        self._pending_flow_state = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FroniusModbusOptionsFlow()

    async def _async_finish_user(self, settings, info, previous_host):
        del previous_host
        await self.async_set_unique_id(_entry_unique_id(settings))
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

    async def async_step_user(self, user_input=None):
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="user",
            password_step_id="user_password",
            defaults=_default_payload(),
            previous_host=None,
            previous_settings=None,
            force_apply_modbus_config=True,
            on_success=self._async_finish_user,
        )

    async def async_step_user_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="user_password",
            restart_step=self.async_step_user,
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
            on_success=self._async_finish_reconfigure,
        )

    async def async_step_reconfigure_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="reconfigure_password",
            restart_step=self.async_step_reconfigure,
            on_success=self._async_finish_reconfigure,
        )


class FroniusModbusOptionsFlow(TokenFlowMixin, config_entries.OptionsFlow):
    """Handle Fronius Modbus options."""

    async def _async_finish_options(self, settings, info, previous_host):
        del info
        unique_id = _claim_host(self.hass, self.config_entry, settings)
        self.hass.config_entries.async_update_entry(
            self.config_entry, unique_id=unique_id, title=_entry_title(settings)
        )
        if previous_host != settings[CONF_HOST]:
            await _async_delete_token(self.hass, previous_host)
        return self.async_create_entry(
            title="",
            data=_entry_payload(settings, reconfigure_required=False),
        )

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
            on_success=self._async_finish_options,
        )

    async def async_step_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="password",
            restart_step=self.async_step_init,
            on_success=self._async_finish_options,
        )
