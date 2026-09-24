from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.const import CONF_HOST
import voluptuous as vol
from homeassistant.helpers import issue_registry as ir

from .config_flow import TokenFlowMixin, async_update_entry_from_input, entry_defaults
from .const import (
    DOMAIN,
    SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
)


class FroniusReconfigureRepairFlow(TokenFlowMixin, RepairsFlow):
    """Repair flow that reuses the reconfigure fields and validation."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._pending_flow_state = None

    def _issue_id(self) -> str:
        return f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{self._entry_id}"

    def _resolve_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id())

    async def _async_finish_repair(
        self,
        settings,
        info,
        previous_host,
    ):
        del info
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            self._resolve_issue()
            return self.async_create_entry(title="", data={})

        await async_update_entry_from_input(
            self.hass,
            entry,
            settings,
            previous_host=previous_host,
        )
        self._resolve_issue()
        return self.async_create_entry(title="", data={})

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            self._resolve_issue()
            return self.async_create_entry(title="", data={})

        defaults = entry_defaults(entry)
        # Home Assistant starts a repair flow by handing the issue's own `data`
        # to the first step as user_input. That is not a form submission, and
        # taking it for one skipped straight past the settings form.
        if user_input is not None and CONF_HOST not in user_input:
            user_input = None
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="init",
            password_step_id="password",
            defaults=defaults,
            previous_host=defaults["host"],
            previous_settings=defaults,
            force_apply_modbus_config=True,
            claim_host=lambda settings: self._async_claim_entry_host(entry, settings),
            on_success=self._async_finish_repair,
        )

    async def async_step_password(self, user_input: dict[str, Any] | None = None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="password",
            restart_step=self.async_step_init,
            on_success=self._async_finish_repair,
        )


class FroniusDisableSolarApiRepairFlow(RepairsFlow):
    """Repair flow that disables Solar API on the inverter."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._pending_flow_state = None

    def _issue_id(self) -> str:
        return f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}{self._entry_id}"

    def _resolve_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id())

    def _description_placeholders(self) -> dict[str, str] | None:
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self._issue_id())
        return issue.translation_placeholders if issue else None

    async def _async_finish_repair(self):
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            self._resolve_issue()
            return self.async_create_entry(title="", data={})

        runtime = getattr(entry, "runtime_data", None)
        web_control = None if runtime is None else runtime.web_control
        if web_control is None or not web_control.configured or runtime.web is None:
            raise RuntimeError("Fronius Web API is not configured")

        await web_control.set_solar_api_enabled(False)
        await runtime.web.async_refresh()
        if runtime.web.data.solar_api_enabled is not False:
            raise RuntimeError("Solar API disable could not be confirmed")
        self._resolve_issue()
        return self.async_create_entry(title="", data={})

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        del user_input
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            self._resolve_issue()
            return self.async_create_entry(title="", data={})

        return self.async_show_menu(
            step_id="init",
            menu_options=["fix", "ignore"],
            description_placeholders=self._description_placeholders(),
        )

    async def async_step_fix(self, user_input: dict[str, Any] | None = None):
        del user_input
        return await self.async_step_confirm()

    async def async_step_ignore(self, user_input: dict[str, Any] | None = None):
        del user_input
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            self._resolve_issue()
            return self.async_create_entry(title="", data={})

        ir.async_ignore_issue(self.hass, DOMAIN, self._issue_id(), True)
        return self.async_abort(reason="issue_ignored")

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None):
        description_placeholders = self._description_placeholders()
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=description_placeholders,
            )
        try:
            return await self._async_finish_repair()
        except Exception:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=description_placeholders,
                errors={"base": "cannot_connect"},
            )


async def async_create_fix_flow(
    hass,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create fix flow for a Fronius repairs issue."""
    if issue_id.startswith(SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX):
        entry_id = str(
            (data or {}).get("entry_id")
            or issue_id.removeprefix(SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX)
        )
        return FroniusDisableSolarApiRepairFlow(entry_id)

    if not issue_id.startswith(MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX):
        raise ValueError(f"Unknown issue: {issue_id}")

    entry_id = str(
        (data or {}).get("entry_id")
        or issue_id.removeprefix(MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX)
    )
    return FroniusReconfigureRepairFlow(entry_id)
