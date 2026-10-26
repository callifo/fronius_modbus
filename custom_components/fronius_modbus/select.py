import logging

from .const import (
    STORAGE_API_SELECT_TYPES,
    STORAGE_MODBUS_SELECT_TYPES,
    INVERTER_SELECT_TYPES,
)

from homeassistant.components.select import (
    SelectEntity,
)

from .hub import Hub
from .base import FroniusModbusBaseEntity, async_ensure_translation_cache

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    await async_ensure_translation_cache(hass)
    hub: Hub = config_entry.runtime_data
    coordinator = hub.coordinator

    entities = []

    if hub.storage_configured:
        for select_info in STORAGE_MODBUS_SELECT_TYPES:
            select = FroniusModbusSelect(
                coordinator=coordinator,
                device_info=hub.device_info_storage,
                name=select_info[0],
                key=select_info[1],
                options=select_info[2],
                translation_key=select_info[0],
                hub=hub,  # Pass hub for control methods
            )
            entities.append(select)

        if hub.web_api_configured:
            for select_info in STORAGE_API_SELECT_TYPES:
                select = FroniusModbusSelect(
                    coordinator=coordinator,
                    device_info=hub.device_info_storage,
                    name=select_info[0],
                    key=select_info[1],
                    options=select_info[2],
                    translation_key=select_info[0],
                    hub=hub,
                )
                entities.append(select)

    # Add inverter select entities.
    for select_info in INVERTER_SELECT_TYPES:
        select = FroniusModbusSelect(
            coordinator=coordinator,
            device_info=hub.device_info_inverter,
            name=select_info[0],
            key=select_info[1],
            options=select_info[2],
            translation_key=select_info[0],
            hub=hub,  # Pass hub for control methods
        )
        entities.append(select)

    async_add_entities(entities)
    return True

def get_key(my_dict, search):
    for k, v in my_dict.items():
        if v == search:
            return k
    return None

class FroniusModbusSelect(FroniusModbusBaseEntity, SelectEntity):
    """Representation of an Battery Storage select."""
    _translation_platform = "select"

    def __init__(self, coordinator, device_info, name, key, options, hub, translation_key=None):
        """Initialize the select entity."""
        super().__init__(
            coordinator=coordinator,
            device_info=device_info,
            name=name,
            key=key,
            translation_key=translation_key,
            options=options,
        )
        self._hub = hub  # Store hub reference for control methods

    @property
    def current_option(self) -> str:
        if self.coordinator.data and self._key in self.coordinator.data:
            return self.coordinator.data[self._key]

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        new_mode = get_key(self._options_dict, option)
        if new_mode is None:
            raise ValueError(f"Unsupported option for {self._key}: {option}")

        if self._key == 'ext_control_mode':
            await self._hub.set_mode(new_mode)
        elif self._key == 'api_battery_mode':
            await self._hub.set_api_battery_mode(new_mode)
        elif self._key == 'ac_limit_enable':
            await self._hub.set_ac_limit_enable(new_mode)
        elif self._key == 'power_factor_enable':
            await self._hub.set_power_factor_enable(new_mode)
        elif self._key == 'conn':
            await self._hub.set_conn_status(new_mode)

        # Update coordinator data will trigger entity updates
        if self.coordinator.data:
            self.coordinator.data[self._key] = option
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._key == 'api_battery_mode':
            return self._hub.web_api_configured and self._hub.storage_configured
        if self._key == 'power_factor_enable':
            return self.coordinator.data.get('power_factor_enable') is not None
        return True
