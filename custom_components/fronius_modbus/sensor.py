"""Platform for sensor integration."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HubConfigEntry
from .const import (
    INVERTER_WEB_SENSOR_TYPES,
    INVERTER_SENSOR_TYPES,
    INVERTER_SYMO_SENSOR_TYPES,
    MPPT_MODULE_SENSOR_TYPES,
    INVERTER_STORAGE_SENSOR_TYPES,
    METER_SENSOR_TYPES,
    SENSOR_STATE_OPTIONS,
    SINGLE_PHASE_UNSUPPORTED_METER_SENSOR_KEYS,
    STORAGE_SENSOR_TYPES,
)
from .hub import Hub
from .base import FroniusModbusBaseEntity, async_ensure_translation_cache

_LOGGER = logging.getLogger(__name__)


def _meter_prefix(unit_id: int) -> str:
    return f"meter_{int(unit_id)}_"

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: HubConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA."""
    await async_ensure_translation_cache(hass)
    hub:Hub = config_entry.runtime_data

    entities = []
    coordinator = hub.coordinator

    for sensor_info in INVERTER_SENSOR_TYPES.values():
        translation_key = sensor_info[0]
        options = SENSOR_STATE_OPTIONS.get(translation_key)
        sensor = FroniusModbusSensor(
            coordinator=coordinator,
            device_info=hub.device_info_inverter,
            name=sensor_info[0],
            key=sensor_info[1],
            translation_key=translation_key,
            device_class=SensorDeviceClass.ENUM if options is not None else sensor_info[2],
            state_class=sensor_info[3],
            unit=sensor_info[4],
            icon=sensor_info[5],
            entity_category=sensor_info[6],
            options=options,
        )
        entities.append(sensor)

    for sensor_info in INVERTER_SYMO_SENSOR_TYPES.values():
        translation_key = sensor_info[0]
        options = SENSOR_STATE_OPTIONS.get(translation_key)
        sensor = FroniusModbusSensor(
            coordinator=coordinator,
            device_info=hub.device_info_inverter,
            name=sensor_info[0],
            key=sensor_info[1],
            translation_key=translation_key,
            device_class=SensorDeviceClass.ENUM if options is not None else sensor_info[2],
            state_class=sensor_info[3],
            unit=sensor_info[4],
            icon=sensor_info[5],
            entity_category=sensor_info[6],
            options=options,
        )
        entities.append(sensor)

    if hub.web_api_configured:
        for sensor_info in INVERTER_WEB_SENSOR_TYPES.values():
            if sensor_info[1] == "export_soft_limit" and not hub.tech_configured:
                continue
            translation_key = sensor_info[0]
            options = SENSOR_STATE_OPTIONS.get(translation_key)
            sensor = FroniusModbusSensor(
                coordinator=coordinator,
                device_info=hub.device_info_inverter,
                name=sensor_info[0],
                key=sensor_info[1],
                translation_key=translation_key,
                device_class=SensorDeviceClass.ENUM if options is not None else sensor_info[2],
                state_class=sensor_info[3],
                unit=sensor_info[4],
                icon=sensor_info[5],
                entity_category=sensor_info[6],
                options=options,
            )
            entities.append(sensor)

    if hub._client.mppt_configured:
        module_count = int(hub._client.mppt_module_count)
        data = coordinator.data if isinstance(coordinator.data, dict) else {}
        visible_module_ids = data.get('mppt_visible_module_ids')
        if (
            not isinstance(visible_module_ids, list)
            or not all(isinstance(module_id, int) for module_id in visible_module_ids)
        ):
            visible_module_ids = list(range(1, module_count + 1))

        for module_id in visible_module_ids:
            if module_id < 1 or module_id > module_count:
                continue
            module_idx = module_id - 1
            for sensor_info in MPPT_MODULE_SENSOR_TYPES:
                key = f'mppt_module_{module_idx}_{sensor_info[1]}'
                if key not in data or data[key] is None:
                    continue
                sensor = FroniusModbusSensor(
                    coordinator=coordinator,
                    device_info=hub.device_info_inverter,
                    name=None,
                    key=key,
                    translation_key=f"mppt_module_{sensor_info[0]}",
                    translation_placeholders={"module": str(module_idx)},
                    device_class=sensor_info[2],
                    state_class=sensor_info[3],
                    unit=sensor_info[4],
                    icon=sensor_info[5],
                    entity_category=sensor_info[6],
                )
                entities.append(sensor)

    if hub.meter_configured:
        for meter_unit_id in hub._client._meter_unit_ids:
            prefix = _meter_prefix(meter_unit_id)
            if f"{prefix}unit_id" not in hub.data:
                continue
            phase_count = hub.data.get(f"{prefix}phase_count")
            for sensor_info in METER_SENSOR_TYPES.values():
                if phase_count == 1 and sensor_info[1] in SINGLE_PHASE_UNSUPPORTED_METER_SENSOR_KEYS:
                    continue
                translation_key = sensor_info[0]
                options = SENSOR_STATE_OPTIONS.get(translation_key)
                sensor = FroniusModbusSensor(
                    coordinator=coordinator,
                    device_info=hub.get_device_info_meter(meter_unit_id),
                    name=sensor_info[0],
                    key=f"{prefix}" + sensor_info[1],
                    translation_key=translation_key,
                    device_class=SensorDeviceClass.ENUM if options is not None else sensor_info[2],
                    state_class=sensor_info[3],
                    unit=sensor_info[4],
                    icon=sensor_info[5],
                    entity_category=sensor_info[6],
                    options=options,
                )
                entities.append(sensor)

    if hub.storage_configured:
        for sensor_info in INVERTER_STORAGE_SENSOR_TYPES.values():
            translation_key = sensor_info[0]
            options = SENSOR_STATE_OPTIONS.get(translation_key)
            sensor = FroniusModbusSensor(
                coordinator=coordinator,
                device_info=hub.device_info_inverter,
                name=sensor_info[0],
                key=sensor_info[1],
                translation_key=translation_key,
                device_class=SensorDeviceClass.ENUM if options is not None else sensor_info[2],
                state_class=sensor_info[3],
                unit=sensor_info[4],
                icon=sensor_info[5],
                entity_category=sensor_info[6],
                options=options,
            )
            entities.append(sensor)

        for sensor_info in STORAGE_SENSOR_TYPES.values():
            translation_key = sensor_info[0]
            options = SENSOR_STATE_OPTIONS.get(translation_key)
            sensor = FroniusModbusSensor(
                coordinator=coordinator,
                device_info=hub.device_info_storage,
                name=sensor_info[0],
                key=sensor_info[1],
                translation_key=translation_key,
                device_class=SensorDeviceClass.ENUM if options is not None else sensor_info[2],
                state_class=sensor_info[3],
                unit=sensor_info[4],
                icon=sensor_info[5],
                entity_category=sensor_info[6],
                options=options,
            )
            entities.append(sensor)
    async_add_entities(entities)
    return True

class FroniusModbusSensor(FroniusModbusBaseEntity, SensorEntity):
    """Representation of an Fronius Modbus Modbus sensor."""
    _translation_platform = "sensor"

    def __init__(
        self,
        coordinator,
        device_info,
        name,
        key,
        translation_key=None,
        translation_placeholders=None,
        device_class=None,
        state_class=None,
        unit=None,
        icon=None,
        entity_category=None,
        options=None,
    ):
        super().__init__(
            coordinator=coordinator,
            device_info=device_info,
            name=name,
            key=key,
            translation_key=translation_key,
            translation_placeholders=translation_placeholders,
            device_class=device_class,
            state_class=state_class,
            unit=unit,
            icon=icon,
            entity_category=entity_category,
            options=options,
        )

    @property
    def state(self):
        """Return the state of the sensor."""
        if self.coordinator.data and self._key in self.coordinator.data:
            value = self.coordinator.data[self._key]
            if isinstance(value, str):
                if len(value) > 255:
                    value = value[:255]
                    _LOGGER.error(f'state length > 255. k: {self._key} v: {value}')
            return value

    @property
    def extra_state_attributes(self):
        return None
