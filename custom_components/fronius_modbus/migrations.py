from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from . import hub
from .const import (
    API_USERNAME,
    CONF_API_USERNAME,
    CONF_METER_UNIT_ID,
    CONF_METER_UNIT_IDS,
    CONF_RECONFIGURE_REQUIRED,
    DOMAIN,
    ENTITY_PREFIX,
    INVERTER_API_BUTTON_TYPES,
    INVERTER_API_SWITCH_TYPES,
    INVERTER_NUMBER_TYPES,
    INVERTER_SELECT_TYPES,
    INVERTER_SENSOR_TYPES,
    INVERTER_STORAGE_SENSOR_TYPES,
    INVERTER_SYMO_SENSOR_TYPES,
    INVERTER_WEB_SENSOR_TYPES,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
    METER_SENSOR_TYPES,
    MPPT_MODULE_SENSOR_TYPES,
    SINGLE_PHASE_UNSUPPORTED_METER_SENSOR_KEYS,
    STORAGE_API_NUMBER_TYPES,
    STORAGE_API_SELECT_TYPES,
    STORAGE_API_SWITCH_TYPES,
    STORAGE_MODBUS_NUMBER_TYPES,
    STORAGE_MODBUS_SELECT_TYPES,
    STORAGE_SENSOR_TYPES,
    TECHNICIAN_USERNAME,
)
from .token_store import async_get_token_store

_LOGGER = logging.getLogger(__name__)
_TRANSLATIONS_DIR = Path(__file__).resolve().parent / "translations"
_TRANSLATION_CACHE: dict[str, dict] = {}

_TARGET_VERSION = 1
_TARGET_MINOR_VERSION = 10

_LEGACY_METER_DEVICE_RE = re.compile(r".*_meter_?\d+")
_V019_MPPT_UNIQUE_ID_MAPPINGS = (
    ("fm_mppt1_power", "mppt_module_0_dc_power", "mppt_module_dc_power", {"module": "0"}),
    ("fm_mppt2_power", "mppt_module_1_dc_power", "mppt_module_dc_power", {"module": "1"}),
    ("fm_mppt1_lfte", "mppt_module_0_lifetime_energy", "mppt_module_lifetime_energy", {"module": "0"}),
    ("fm_mppt2_lfte", "mppt_module_1_lifetime_energy", "mppt_module_lifetime_energy", {"module": "1"}),
    ("fm_mppt3_power", "storage_charge_power", "storage_charge_power", None),
    ("fm_mppt4_power", "storage_discharge_power", "storage_discharge_power", None),
    ("fm_mppt3_lfte", "storage_charge_lfte", "storage_charge_lfte", None),
    ("fm_mppt4_lfte", "storage_discharge_lfte", "storage_discharge_lfte", None),
)

_INVERTER_ENTITY_DEFINITIONS = (
    INVERTER_SELECT_TYPES,
    INVERTER_NUMBER_TYPES,
    INVERTER_SENSOR_TYPES,
    INVERTER_SYMO_SENSOR_TYPES,
)
_WEB_INVERTER_ENTITY_DEFINITIONS = (
    INVERTER_WEB_SENSOR_TYPES,
    INVERTER_API_SWITCH_TYPES,
    INVERTER_API_BUTTON_TYPES,
)
_STORAGE_ENTITY_DEFINITIONS = (
    STORAGE_MODBUS_SELECT_TYPES,
    STORAGE_MODBUS_NUMBER_TYPES,
    INVERTER_STORAGE_SENSOR_TYPES,
    STORAGE_SENSOR_TYPES,
)
_WEB_STORAGE_ENTITY_DEFINITIONS = (
    STORAGE_API_SELECT_TYPES,
    STORAGE_API_NUMBER_TYPES,
    STORAGE_API_SWITCH_TYPES,
)


def _entry_value(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


def _entity_entries_for_config_entry(registry, entry: ConfigEntry):
    return list(er.async_entries_for_config_entry(registry, entry.entry_id))


def _migration_issue_id(entry: ConfigEntry) -> str:
    return f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{entry.entry_id}"


def _legacy_device_identifiers(entry: ConfigEntry) -> set[str]:
    name = str(_entry_value(entry, CONF_NAME, "Fronius"))
    return {
        f"{name}_inverter",
        f"{name}_battery_storage",
    }


def _legacy_device_needs_removal(entry: ConfigEntry, device) -> bool:
    identifiers = getattr(device, "identifiers", set())
    return any(
        identifier_domain == DOMAIN
        and (
            identifier in _legacy_device_identifiers(entry)
            or _LEGACY_METER_DEVICE_RE.fullmatch(identifier)
        )
        for identifier_domain, identifier in identifiers
    )


def _entity_unique_id(runtime_data: hub.Hub, key: str) -> str:
    return f"{runtime_data.entity_prefix}_{key}"


def _legacy_entity_unique_id(entry: ConfigEntry, key: str) -> str:
    name = str(_entry_value(entry, CONF_NAME, "Fronius")).lower()
    return f"{ENTITY_PREFIX}_{name}__{key}"


def _entry_instance_key(entry: ConfigEntry) -> str:
    return hub.Hub._normalize_instance_key(entry.entry_id)


def _updated_entry_title(entry: ConfigEntry) -> str:
    name = str(_entry_value(entry, CONF_NAME, "Fronius")).strip() or "Fronius"
    host = str(_entry_value(entry, CONF_HOST, "")).strip()
    return f"{name} {host}" if host else name


def _definition_keys(definitions) -> list[str]:
    items = definitions.values() if isinstance(definitions, dict) else definitions
    return [item[1] for item in items]


def _load_translation_data(language: str) -> dict:
    return _TRANSLATION_CACHE.get(language, {})


def _read_translation_data(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


async def _async_load_translation_data(hass: HomeAssistant, language: str) -> dict:
    if language not in _TRANSLATION_CACHE:
        path = _TRANSLATIONS_DIR / f"{language}.json"
        _TRANSLATION_CACHE[language] = await hass.async_add_executor_job(
            _read_translation_data,
            path,
        )
    return _TRANSLATION_CACHE[language]


async def _async_translated_sensor_name(
    hass: HomeAssistant,
    translation_key: str,
    placeholders: dict[str, str] | None = None,
) -> str:
    language_candidates: list[str] = []
    language = getattr(hass.config, "language", None)
    if isinstance(language, str) and language:
        language_candidates.append(language)
        if "-" in language:
            language_candidates.append(language.split("-", 1)[0])
    language_candidates.append("en")

    for candidate in language_candidates:
        name = (
            (await _async_load_translation_data(hass, candidate))
            .get("entity", {})
            .get("sensor", {})
            .get(translation_key, {})
            .get("name")
        )
        if not isinstance(name, str):
            continue
        if placeholders:
            try:
                return name.format(**placeholders)
            except KeyError:
                return name
        return name
    return translation_key


def _add_expected_keys(expected: set[str], runtime_data: hub.Hub, keys) -> None:
    for key in keys:
        expected.add(_entity_unique_id(runtime_data, key))


def _visible_mppt_module_ids(runtime_data: hub.Hub, data: dict[str, object]) -> list[int]:
    module_count = int(runtime_data._client.mppt_module_count)
    visible_module_ids = data.get("mppt_visible_module_ids")
    if (
        not isinstance(visible_module_ids, list)
        or not all(isinstance(module_id, int) for module_id in visible_module_ids)
    ):
        return list(range(1, module_count + 1))
    return visible_module_ids


def _expected_mppt_unique_ids(runtime_data: hub.Hub, data: dict[str, object]) -> set[str]:
    expected: set[str] = set()
    if not runtime_data._client.mppt_configured:
        return expected

    module_count = int(runtime_data._client.mppt_module_count)
    for module_id in _visible_mppt_module_ids(runtime_data, data):
        if module_id < 1 or module_id > module_count:
            continue
        module_idx = module_id - 1
        for _name, key_suffix, *_rest in MPPT_MODULE_SENSOR_TYPES:
            key = f"mppt_module_{module_idx}_{key_suffix}"
            if key in data and data[key] is not None:
                expected.add(_entity_unique_id(runtime_data, key))
    return expected


def _expected_meter_unique_ids(runtime_data: hub.Hub, data: dict[str, object]) -> set[str]:
    expected: set[str] = set()
    if not runtime_data.meter_configured:
        return expected

    for meter_unit_id in runtime_data._client._meter_unit_ids:
        prefix = f"meter_{int(meter_unit_id)}_"
        if f"{prefix}unit_id" not in data:
            continue

        phase_count = data.get(f"{prefix}phase_count")
        for sensor_info in METER_SENSOR_TYPES.values():
            if phase_count == 1 and sensor_info[1] in SINGLE_PHASE_UNSUPPORTED_METER_SENSOR_KEYS:
                continue
            expected.add(_entity_unique_id(runtime_data, f"{prefix}{sensor_info[1]}"))
    return expected


def _expected_entity_unique_ids(runtime_data: hub.Hub) -> set[str]:
    expected: set[str] = set()
    data = runtime_data.data if isinstance(runtime_data.data, dict) else {}

    for definitions in _INVERTER_ENTITY_DEFINITIONS:
        _add_expected_keys(expected, runtime_data, _definition_keys(definitions))

    if runtime_data.web_api_configured:
        for definitions in _WEB_INVERTER_ENTITY_DEFINITIONS:
            _add_expected_keys(expected, runtime_data, _definition_keys(definitions))
        if not runtime_data.tech_configured:
            expected.discard(_entity_unique_id(runtime_data, "export_soft_limit"))

    if runtime_data.storage_configured:
        for definitions in _STORAGE_ENTITY_DEFINITIONS:
            _add_expected_keys(expected, runtime_data, _definition_keys(definitions))

        if runtime_data.web_api_configured:
            for definitions in _WEB_STORAGE_ENTITY_DEFINITIONS:
                _add_expected_keys(expected, runtime_data, _definition_keys(definitions))

    expected.update(_expected_mppt_unique_ids(runtime_data, data))
    expected.update(_expected_meter_unique_ids(runtime_data, data))

    return expected


async def _async_set_reconfigure_required(
    hass: HomeAssistant,
    entry: ConfigEntry,
    required: bool,
) -> None:
    new_data = dict(entry.data)
    new_options = dict(entry.options)
    changed = False

    if new_data.get(CONF_RECONFIGURE_REQUIRED) != required:
        new_data[CONF_RECONFIGURE_REQUIRED] = required
        changed = True
    if new_options.get(CONF_RECONFIGURE_REQUIRED) != required:
        new_options[CONF_RECONFIGURE_REQUIRED] = required
        changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries from 32cd901 to the current shape."""
    _LOGGER.debug(
        "Migrating config entry %s version=%s minor=%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )

    if entry.version > _TARGET_VERSION:
        _LOGGER.error("Unsupported config entry version: %s", entry.version)
        return False

    if entry.version == _TARGET_VERSION and entry.minor_version < 9:
        new_data = dict(entry.data)
        new_options = dict(entry.options)

        new_data.pop(CONF_METER_UNIT_ID, None)
        new_data.pop(CONF_METER_UNIT_IDS, None)
        new_options.pop(CONF_METER_UNIT_ID, None)
        new_options.pop(CONF_METER_UNIT_IDS, None)
        new_data[CONF_RECONFIGURE_REQUIRED] = True
        new_options[CONF_RECONFIGURE_REQUIRED] = True

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=new_options,
            version=_TARGET_VERSION,
            minor_version=9,
            title=_updated_entry_title(entry),
        )

    if entry.version == _TARGET_VERSION and entry.minor_version < _TARGET_MINOR_VERSION:
        host = str(_entry_value(entry, CONF_HOST, "")).strip()
        api_username = API_USERNAME
        if host and await async_get_token_store(hass).async_has_token(
            host, TECHNICIAN_USERNAME
        ):
            api_username = TECHNICIAN_USERNAME

        new_data = dict(entry.data)
        new_options = dict(entry.options)
        new_data[CONF_API_USERNAME] = api_username
        new_options[CONF_API_USERNAME] = api_username
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=new_options,
            version=_TARGET_VERSION,
            minor_version=_TARGET_MINOR_VERSION,
        )

    return True


async def async_prepare_entry_token(
    hass: HomeAssistant,
    entry: ConfigEntry,
    host: str,
) -> dict[str, str] | None:
    api_username = str(_entry_value(entry, CONF_API_USERNAME, API_USERNAME))
    token = await async_get_token_store(hass).async_load_token(host, api_username)
    await _async_set_reconfigure_required(hass, entry, not bool(token))
    return token


async def async_sync_reconfigure_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    has_token: bool,
) -> None:
    issue_id = _migration_issue_id(entry)
    needs_reconfigure = bool(_entry_value(entry, CONF_RECONFIGURE_REQUIRED, False)) or not has_token
    if needs_reconfigure:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="legacy_modbus_only_entry_reconfigure",
            translation_placeholders={
                "entry_title": entry.title or _entry_value(entry, CONF_NAME, "Fronius"),
            },
            data={"entry_id": entry.entry_id},
        )
        return

    ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_migrate_v019_mppt_statistics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: hub.Hub,
) -> None:
    """Rename old v0.1.9 MPPT entities so recorder keeps history/statistics."""
    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_entries = _entity_entries_for_config_entry(registry, entry)
    old_unique_ids = {candidate.unique_id or "" for candidate in entity_entries}
    expected_unique_ids = _expected_entity_unique_ids(runtime_data)
    reserved_entity_ids = {candidate.entity_id for candidate in entity_entries}

    for old_unique_id, new_key, translation_key, placeholders in _V019_MPPT_UNIQUE_ID_MAPPINGS:
        if old_unique_id not in old_unique_ids:
            continue

        new_unique_id = _entity_unique_id(runtime_data, new_key)
        if new_unique_id not in expected_unique_ids:
            continue

        old_entity_id = registry.async_get_entity_id("sensor", DOMAIN, old_unique_id)
        if old_entity_id is None:
            continue

        if registry.async_get_entity_id("sensor", DOMAIN, new_unique_id) is not None:
            _LOGGER.debug(
                "Skipping v0.1.9 MPPT migration for %s because target unique id %s already exists",
                old_entity_id,
                new_unique_id,
            )
            continue

        entity_entry = registry.async_get(old_entity_id)
        if entity_entry is None:
            continue

        sensor_name = await _async_translated_sensor_name(hass, translation_key, placeholders)
        suggested_object_id = sensor_name
        if entity_entry.device_id and (device_entry := device_registry.async_get(entity_entry.device_id)):
            device_name = device_entry.name_by_user or device_entry.name
            if device_name:
                suggested_object_id = f"{device_name} {sensor_name}"

        reserved_entity_ids.discard(old_entity_id)
        new_entity_id = registry.async_get_available_entity_id(
            "sensor",
            suggested_object_id,
            current_entity_id=old_entity_id,
            reserved_entity_ids=reserved_entity_ids,
        )
        reserved_entity_ids.add(new_entity_id)

        registry.async_update_entity(
            old_entity_id,
            new_entity_id=new_entity_id,
            new_unique_id=new_unique_id,
        )
        _LOGGER.info(
            "Migrated v0.1.9 MPPT entity %s -> %s to preserve statistics/history",
            old_entity_id,
            new_entity_id,
        )


async def async_migrate_name_based_unique_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: hub.Hub,
) -> None:
    """Move old name-based unique ids to stable per-entry ids."""
    registry = er.async_get(hass)
    entity_entries = _entity_entries_for_config_entry(registry, entry)
    current_unique_ids = {candidate.unique_id or "" for candidate in entity_entries}
    expected_unique_ids = _expected_entity_unique_ids(runtime_data)
    migrated = 0

    for new_unique_id in expected_unique_ids:
        key = new_unique_id.removeprefix(f"{runtime_data.entity_prefix}_")
        if key == new_unique_id:
            continue

        old_unique_id = _legacy_entity_unique_id(entry, key)
        if old_unique_id not in current_unique_ids or new_unique_id in current_unique_ids:
            continue

        entity_entry = next(
            (candidate for candidate in entity_entries if (candidate.unique_id or "") == old_unique_id),
            None,
        )
        if entity_entry is None:
            continue

        registry.async_update_entity(entity_entry.entity_id, new_unique_id=new_unique_id)
        current_unique_ids.discard(old_unique_id)
        current_unique_ids.add(new_unique_id)
        migrated += 1

    if migrated:
        _LOGGER.info(
            "Migrated %s entities from name-based unique ids to per-entry ids for %s",
            migrated,
            _entry_instance_key(entry),
        )


async def async_remove_legacy_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove device ids that only existed on older layouts."""
    device_registry = dr.async_get(hass)

    removed_devices = 0
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if _legacy_device_needs_removal(entry, device):
            device_registry.async_remove_device(device.id)
            removed_devices += 1

    if removed_devices:
        _LOGGER.info("Removed %s legacy meter devices from pre-web-api config", removed_devices)


async def async_remove_unexpected_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: hub.Hub,
) -> None:
    """Remove registry entities that the integration no longer creates."""
    registry = er.async_get(hass)
    expected_unique_ids = _expected_entity_unique_ids(runtime_data)
    removed = 0
    for entity_entry in _entity_entries_for_config_entry(registry, entry):
        unique_id = entity_entry.unique_id or ""
        if not unique_id or unique_id in expected_unique_ids:
            continue
        registry.async_remove(entity_entry.entity_id)
        removed += 1

    if removed:
        _LOGGER.info(
            "Removed %s stale entities that are no longer registered by the integration",
            removed,
        )
