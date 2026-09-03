from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
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
    DEFAULT_AUTO_ENABLE_MODBUS,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_METER_UNIT_IDS,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    API_USERNAME,
    API_USERNAMES,
    TECHNICIAN_USERNAME,
    SUPPORTED_MANUFACTURERS,
    SUPPORTED_MODELS,
)
from .froniuswebclient import ClientIpResolutionError, mint_token
from .hub import Hub
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
    payload[CONF_API_USERNAME] = str(
        user_input.get(CONF_API_USERNAME, payload[CONF_API_USERNAME])
    ).strip().lower()
    payload.pop(CONF_API_PASSWORD, None)
    payload.pop("meter_modbus_unit_id", None)
    payload.pop("meter_modbus_unit_ids", None)
    return payload


def _entry_payload(data: dict[str, Any], *, reconfigure_required: bool) -> dict[str, Any]:
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
    except (TypeError, ValueError):
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


def _build_password_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_API_PASSWORD): TextSelector(
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
    else:
        _LOGGER.exception("Unexpected exception")
        errors["base"] = "unknown"


def _validate_static_input(data: dict[str, Any]) -> None:
    if len(data[CONF_HOST]) < 3:
        raise _InvalidHost
    if data[CONF_PORT] > 65535:
        raise _InvalidPort
    if data[CONF_SCAN_INTERVAL] < 5:
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
    hass: HomeAssistant,
    host: str,
    username: str,
) -> dict[str, str] | None:
    return await async_get_token_store(hass).async_load_token(host, username)


async def _async_save_token(
    hass: HomeAssistant,
    host: str,
    username: str,
    token: dict[str, str],
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

    hub = Hub(
        hass,
        data[CONF_NAME],
        data[CONF_HOST],
        data[CONF_PORT],
        data[CONF_INVERTER_UNIT_ID],
        list(DEFAULT_METER_UNIT_IDS),
        data[CONF_SCAN_INTERVAL],
        api_username=data[CONF_API_USERNAME],
        api_password=api_password or None,
        api_token=api_token,
        auto_enable_modbus=data.get(CONF_AUTO_ENABLE_MODBUS, DEFAULT_AUTO_ENABLE_MODBUS),
        restrict_modbus_to_this_ip=data.get(
            CONF_RESTRICT_MODBUS_TO_THIS_IP,
            DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
        ),
    )
    try:
        if not await hub.validate_web_api():
            raise _InvalidApiCredentials
        await hub.init_data(
            setup_coordinator=False,
            apply_modbus_config=apply_modbus_config,
        )
    except ClientIpResolutionError:
        raise _CannotResolveLocalIp
    except _InvalidApiCredentials:
        raise
    except Exception as err:
        _LOGGER.error("Cannot start hub %s", err)
        raise _CannotConnect from err
    finally:
        hub.close()

    manufacturer = hub.data.get("i_manufacturer")
    if manufacturer is None:
        _LOGGER.error("No manufacturer is returned")
        raise _UnsupportedHardware
    if manufacturer not in SUPPORTED_MANUFACTURERS:
        _LOGGER.error("Unsupported manufacturer: %r", manufacturer)
        raise _UnsupportedHardware

    model = hub.data.get("i_model")
    if model is None:
        _LOGGER.error("No model type is returned")
        raise _UnsupportedHardware

    if not any(model.startswith(supported_model) for supported_model in SUPPORTED_MODELS):
        _LOGGER.warning("Untested model %s", model)

    return {"title": _entry_title(data)}


async def async_update_entry_from_input(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    validated_input: dict[str, Any],
    *,
    previous_host: str | None = None,
) -> None:
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
            data_schema=_build_password_schema(),
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
        on_success: _FlowFinishCallback,
    ):
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                settings = _expand_settings_input(user_input, defaults)
                _validate_static_input(settings)
                apply_modbus_config = force_apply_modbus_config or _should_apply_modbus_config(
                    settings,
                    previous_settings,
                )
                token = await _async_load_token(
                    self.hass,
                    settings[CONF_HOST],
                    settings[CONF_API_USERNAME],
                )
                if token is None:
                    self._pending_flow_state = _PendingFlowState(
                        settings,
                        previous_host,
                        apply_modbus_config,
                    )
                    return await self._async_show_password_step(step_id=password_step_id)

                info = await _validate_input(
                    self.hass,
                    settings,
                    api_token=token,
                    apply_modbus_config=apply_modbus_config,
                )
                self._pending_flow_state = None
                return await on_success(settings, info, previous_host)
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
                token = await _async_mint_token(
                    self.hass,
                    state.settings[CONF_HOST],
                    user_input.get(CONF_API_PASSWORD, ""),
                    username=state.settings[CONF_API_USERNAME],
                )
                await _async_save_token(
                    self.hass,
                    state.settings[CONF_HOST],
                    state.settings[CONF_API_USERNAME],
                    token,
                )
                info = await _validate_input(
                    self.hass,
                    state.settings,
                    api_token=token,
                    apply_modbus_config=state.apply_modbus_config,
                )
                self._pending_flow_state = None
                return await on_success(state.settings, info, state.previous_host)
            except Exception as err:  # pylint: disable=broad-except
                _set_form_error(errors, err)

        return await self._async_show_password_step(step_id=step_id, errors=errors)


class ConfigFlow(TokenFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1
    MINOR_VERSION = 9
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
            on_success=self._async_finish_options,
        )

    async def async_step_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="password",
            restart_step=self.async_step_init,
            on_success=self._async_finish_options,
        )
